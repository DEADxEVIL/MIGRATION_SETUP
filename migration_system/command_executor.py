"""
command_executor.py
===================

SINGLE COMMAND EXECUTION LAYER.

ALL command execution goes through this.
Uses SSHManager for SSH connections.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union, Callable

from .ssh_manager import SSHManager, SSHCredentials

logger = logging.getLogger("command_executor")


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float
    target: str = "local"
    command: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_seconds": round(self.duration_seconds, 2),
            "target": self.target,
            "command": self.command,
            "succeeded": self.succeeded,
            "metadata": self.metadata,
        }


class CommandExecutor:
    """
    SINGLE COMMAND EXECUTION LAYER.
    
    ALL command execution goes through this.
    Uses SSHManager for SSH connections.
    """

    def __init__(self):
        self._ssh_managers: Dict[str, SSHManager] = {}
        self._agent_connections: Dict[str, socket.socket] = {}
        self._lock = threading.RLock()
        self._shutdown = False

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #

    def register_ssh_credentials(self, target: str, credentials: SSHCredentials) -> None:
        """Register SSH credentials for a target."""
        with self._lock:
            if target in self._ssh_managers:
                self._ssh_managers[target].disconnect()

            ssh = SSHManager(credentials, connect_timeout=15)
            self._ssh_managers[target] = ssh
            logger.info(f"Registered SSH credentials for target: {target}")

    def register_agent_connection(self, target: str, host: str, port: int) -> None:
        """Register a migration agent connection."""
        with self._lock:
            if target in self._agent_connections:
                try:
                    self._agent_connections[target].close()
                except Exception:
                    pass

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((host, port))
            self._agent_connections[target] = sock
            logger.info(f"Registered agent connection for target: {target}")

    # ------------------------------------------------------------------ #
    # Command execution - THE ONLY WAY TO EXECUTE COMMANDS
    # ------------------------------------------------------------------ #

    def execute_local(self, command: str, timeout: float = 300.0, shell: bool = True) -> CommandResult:
        """Execute a command locally."""
        start = time.time()
        try:
            if shell:
                result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
            else:
                result = subprocess.run(command.split(), capture_output=True, text=True, timeout=timeout)

            return CommandResult(
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                duration_seconds=time.time() - start,
                target="local",
                command=command,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                stdout="", stderr=f"Command timed out after {timeout}s",
                exit_code=-1, duration_seconds=time.time() - start,
                target="local", command=command,
                metadata={"timeout": timeout},
            )
        except Exception as e:
            return CommandResult(
                stdout="", stderr=str(e), exit_code=-1,
                duration_seconds=time.time() - start,
                target="local", command=command,
            )

    def execute_ssh(
        self,
        target: str,
        command: str,
        use_sudo: bool = False,
        timeout: float = 300.0,
        retries: int = 2,
    ) -> CommandResult:
        """
        Execute a command via SSH.
        
        This is the ONLY way to execute SSH commands.
        Uses SSHManager for connection management.
        """
        start = time.time()

        with self._lock:
            if target not in self._ssh_managers:
                return CommandResult(
                    stdout="", stderr=f"No SSH credentials registered for target: {target}",
                    exit_code=-1, duration_seconds=time.time() - start,
                    target="ssh", command=command,
                )

            ssh = self._ssh_managers[target]

        try:
            # Ensure connection
            ssh.ensure_connected()
            transport = ssh.get_transport()
            if transport is None:
                raise Exception("No active transport")

            # Build command with sudo if needed
            if use_sudo:
                full_command = f"sudo -S -p '' bash -lc {command!r}"
            else:
                full_command = command

            # Execute command via SSH channel
            channel = transport.open_session(timeout=15)
            channel.settimeout(timeout)
            channel.exec_command(full_command)

            # Send sudo password if needed
            if use_sudo and ssh.credentials.password:
                channel.sendall(f"{ssh.credentials.password}\n".encode())

            # Read output
            stdout = ""
            stderr = ""
            while True:
                if channel.recv_ready():
                    stdout += channel.recv(65536).decode(errors="replace")
                if channel.recv_stderr_ready():
                    stderr += channel.recv_stderr(65536).decode(errors="replace")
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                time.sleep(0.05)

            exit_code = channel.recv_exit_status()
            channel.close()

            return CommandResult(
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration_seconds=time.time() - start,
                target="ssh",
                command=command,
            )

        except Exception as e:
            return CommandResult(
                stdout="", stderr=str(e), exit_code=-1,
                duration_seconds=time.time() - start,
                target="ssh", command=command,
            )

    def execute_agent(self, target: str, command: str, timeout: float = 300.0) -> CommandResult:
        """Execute a command via Migration Agent."""
        start = time.time()

        with self._lock:
            if target not in self._agent_connections:
                return CommandResult(
                    stdout="", stderr=f"No agent connection for target: {target}",
                    exit_code=-1, duration_seconds=time.time() - start,
                    target="agent", command=command,
                )

            sock = self._agent_connections[target]

        try:
            request = json.dumps({"type": "shell", "command": command, "timeout": timeout})
            sock.sendall((request + "\n").encode("utf-8"))

            sock.settimeout(timeout + 5)
            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk

            response_line = buffer.split(b"\n", 1)[0] if b"\n" in buffer else buffer
            response = json.loads(response_line.decode("utf-8"))

            return CommandResult(
                stdout=response.get("stdout", ""),
                stderr=response.get("stderr", ""),
                exit_code=response.get("exit_code", -1),
                duration_seconds=time.time() - start,
                target="agent",
                command=command,
                metadata=response.get("metadata", {}),
            )
        except socket.timeout:
            return CommandResult(
                stdout="", stderr=f"Agent command timed out after {timeout}s",
                exit_code=-1, duration_seconds=time.time() - start,
                target="agent", command=command,
            )
        except Exception as e:
            return CommandResult(
                stdout="", stderr=str(e), exit_code=-1,
                duration_seconds=time.time() - start,
                target="agent", command=command,
            )

    def execute(self, target: str, command: str, use_sudo: bool = False, timeout: float = 300.0, retries: int = 2) -> CommandResult:
        """
        Execute command on target.
        
        Targets:
        - "local" - execute locally
        - "ssh:target_name" - execute via SSH
        - "agent:target_name" - execute via Agent
        """
        if target == "local":
            return self.execute_local(command, timeout)

        if target.startswith("ssh:"):
            return self.execute_ssh(target[4:], command, use_sudo, timeout, retries)

        if target.startswith("agent:"):
            return self.execute_agent(target[6:], command, timeout)

        return CommandResult(
            stdout="", stderr=f"Unknown target: {target}",
            exit_code=-1, duration_seconds=0.0,
            target=target, command=command,
        )

    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #

    def cleanup(self) -> None:
        self._shutdown = True
        with self._lock:
            for ssh in self._ssh_managers.values():
                try:
                    ssh.disconnect()
                except Exception:
                    pass
            self._ssh_managers.clear()

            for sock in self._agent_connections.values():
                try:
                    sock.close()
                except Exception:
                    pass
            self._agent_connections.clear()

        logger.info("CommandExecutor cleaned up")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
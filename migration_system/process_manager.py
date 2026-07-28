"""
process_manager.py
==================

Manages process lifecycle on a host using CommandExecutor.

Supports:
    - systemd (preferred for production)
    - screen
    - tmux
    - nohup (simple background)
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from .command_executor import CommandExecutor, CommandResult

logger = logging.getLogger("process_manager")


class LaunchStrategy(str, Enum):
    SYSTEMD = "systemd"
    SCREEN = "screen"
    TMUX = "tmux"
    NOHUP = "nohup"


@dataclass
class ProcessInfo:
    pid: int
    name: str
    command: str
    status: str
    cpu_percent: float = 0.0
    mem_percent: float = 0.0
    start_time: Optional[float] = None


@dataclass
class ProcessHandle:
    identifier: str
    strategy: LaunchStrategy
    pid: Optional[int] = None
    working_directory: str = "/opt"
    log_file: Optional[str] = None


class ProcessManagerError(Exception):
    """Raised for process management errors."""


class ProcessManager:
    """Process lifecycle manager using CommandExecutor."""

    def __init__(
        self,
        command_executor: CommandExecutor,
        target: str = "destination",
    ):
        self.executor = command_executor
        self.target = target
        self._managed: Dict[str, ProcessHandle] = {}

    def start(
        self,
        command: str,
        name: str,
        strategy: LaunchStrategy = LaunchStrategy.SYSTEMD,
        working_directory: str = "/opt",
        systemd_user: str = "root",
        restart_policy: str = "always",
        log_file: Optional[str] = None,
    ) -> ProcessHandle:
        """Start a process using the specified strategy."""
        logger.info("Starting process '%s' with strategy %s", name, strategy.value)

        if strategy == LaunchStrategy.SYSTEMD:
            return self._start_systemd(command, name, working_directory, systemd_user, restart_policy)
        elif strategy == LaunchStrategy.SCREEN:
            return self._start_screen(command, name, working_directory, log_file)
        elif strategy == LaunchStrategy.TMUX:
            return self._start_tmux(command, name, working_directory, log_file)
        elif strategy == LaunchStrategy.NOHUP:
            return self._start_nohup(command, name, working_directory, log_file)
        else:
            raise ProcessManagerError(f"Unsupported strategy: {strategy}")

    def _start_systemd(self, command: str, name: str, working_directory: str, user: str, restart_policy: str) -> ProcessHandle:
        unit_path = f"/etc/systemd/system/{name}.service"
        unit_content = (
            f"[Unit]\n"
            f"Description={name} (managed by migration system)\n"
            f"After=network.target\n\n"
            f"[Service]\n"
            f"Type=simple\n"
            f"User={user}\n"
            f"WorkingDirectory={working_directory}\n"
            f"ExecStart={command}\n"
            f"Restart={restart_policy}\n"
            f"RestartSec=5\n"
            f"StandardOutput=journal\n"
            f"StandardError=journal\n\n"
            f"[Install]\n"
            f"WantedBy=multi-user.target\n"
        )

        write_cmd = f"cat > {unit_path} << 'EOF'\n{unit_content}\nEOF"
        result = self.executor.execute(self.target, write_cmd, use_sudo=True)
        if not result.succeeded:
            raise ProcessManagerError(f"Failed to write systemd unit: {result.stderr}")

        for cmd in (f"systemctl daemon-reload", f"systemctl enable {name}", f"systemctl start {name}"):
            result = self.executor.execute(self.target, cmd, use_sudo=True)
            if not result.succeeded:
                raise ProcessManagerError(f"systemd command failed: {cmd} -> {result.stderr}")

        pid_result = self.executor.execute(self.target, f"systemctl show {name} --property=MainPID --value")
        pid = int(pid_result.stdout.strip()) if pid_result.stdout.strip().isdigit() else None

        handle = ProcessHandle(identifier=name, strategy=LaunchStrategy.SYSTEMD, pid=pid)
        self._managed[name] = handle
        logger.info("Started systemd service '%s' with PID %s", name, pid)
        return handle

    def _start_screen(self, command: str, name: str, working_directory: str, log_file: Optional[str]) -> ProcessHandle:
        log_file = log_file or f"/tmp/screen_{name}.log"
        cmd = f"cd {working_directory} && screen -dmS {name} bash -c {command!r} 2>&1 | tee {log_file}"
        result = self.executor.execute(self.target, cmd)
        if not result.succeeded:
            raise ProcessManagerError(f"Failed to start screen session: {result.stderr}")

        handle = ProcessHandle(identifier=name, strategy=LaunchStrategy.SCREEN, working_directory=working_directory)
        self._managed[name] = handle
        logger.info("Started screen session '%s'", name)
        return handle

    def _start_tmux(self, command: str, name: str, working_directory: str, log_file: Optional[str]) -> ProcessHandle:
        log_file = log_file or f"/tmp/tmux_{name}.log"
        cmd = f"cd {working_directory} && tmux new-session -d -s {name} {command!r} 2>&1 | tee {log_file}"
        result = self.executor.execute(self.target, cmd)
        if not result.succeeded:
            raise ProcessManagerError(f"Failed to start tmux session: {result.stderr}")

        handle = ProcessHandle(identifier=name, strategy=LaunchStrategy.TMUX, working_directory=working_directory)
        self._managed[name] = handle
        logger.info("Started tmux session '%s'", name)
        return handle

    def _start_nohup(self, command: str, name: str, working_directory: str, log_file: Optional[str]) -> ProcessHandle:
        log_file = log_file or f"{working_directory}/{name}.log"
        cmd = f"cd {working_directory} && nohup {command} >> {log_file} 2>&1 & echo $!"
        result = self.executor.execute(self.target, cmd)
        if not result.succeeded:
            raise ProcessManagerError(f"Failed to start nohup process: {result.stderr}")

        pid_match = re.search(r"(\d+)", result.stdout.strip())
        pid = int(pid_match.group(1)) if pid_match else None

        handle = ProcessHandle(
            identifier=name,
            strategy=LaunchStrategy.NOHUP,
            pid=pid,
            working_directory=working_directory,
            log_file=log_file
        )
        self._managed[name] = handle
        logger.info("Started nohup process '%s' with PID %s", name, pid)
        return handle

    def stop(self, handle: ProcessHandle, force: bool = False) -> None:
        logger.info("Stopping process '%s' (%s)", handle.identifier, handle.strategy.value)

        if handle.strategy == LaunchStrategy.SYSTEMD:
            cmd = f"systemctl stop {handle.identifier}"
            result = self.executor.execute(self.target, cmd, use_sudo=True)
            if not result.succeeded and not force:
                raise ProcessManagerError(f"Failed to stop systemd service: {result.stderr}")

        elif handle.strategy == LaunchStrategy.SCREEN:
            cmd = f"screen -S {handle.identifier} -X quit"
            result = self.executor.execute(self.target, cmd)
            if not result.succeeded and not force:
                raise ProcessManagerError(f"Failed to stop screen session: {result.stderr}")

        elif handle.strategy == LaunchStrategy.TMUX:
            cmd = f"tmux kill-session -t {handle.identifier}"
            result = self.executor.execute(self.target, cmd)
            if not result.succeeded and not force:
                raise ProcessManagerError(f"Failed to stop tmux session: {result.stderr}")

        elif handle.strategy == LaunchStrategy.NOHUP:
            if handle.pid is None:
                raise ProcessManagerError("Cannot stop nohup process without PID")
            cmd = f"kill {'-9' if force else '-TERM'} {handle.pid} 2>/dev/null || true"
            self.executor.execute(self.target, cmd)

        logger.info("Stopped process '%s'", handle.identifier)

    def restart(self, handle: ProcessHandle, command: Optional[str] = None) -> ProcessHandle:
        logger.info("Restarting process '%s'", handle.identifier)

        if handle.strategy == LaunchStrategy.SYSTEMD:
            result = self.executor.execute(self.target, f"systemctl restart {handle.identifier}", use_sudo=True)
            if not result.succeeded:
                raise ProcessManagerError(f"Failed to restart systemd service: {result.stderr}")
            return handle

        self.stop(handle, force=True)

        if command is None:
            raise ProcessManagerError("Command required to restart non-systemd process")

        return self.start(command, handle.identifier, handle.strategy, handle.working_directory)

    def get_pid_by_name(self, process_name: str) -> List[int]:
        result = self.executor.execute(self.target, f"pgrep -f {process_name!r}")
        if result.exit_code not in (0, 1):
            raise ProcessManagerError(f"pgrep failed: {result.stderr}")

        pids = []
        for line in result.stdout.split():
            if line.strip().isdigit():
                pids.append(int(line))
        return pids

    def is_running(self, pid: int) -> bool:
        result = self.executor.execute(self.target, f"kill -0 {pid} 2>/dev/null")
        return result.succeeded

    def get_process_info(self, pid: int) -> Optional[ProcessInfo]:
        if not self.is_running(pid):
            return None

        result = self.executor.execute(self.target, f"ps -p {pid} -o comm=,args=,pcpu=,pmem=,lstart= 2>/dev/null")
        if not result.succeeded:
            return None

        parts = result.stdout.strip().split(None, 4)
        if len(parts) < 4:
            return None

        return ProcessInfo(
            pid=pid,
            name=parts[0],
            command=parts[1] if len(parts) > 1 else parts[0],
            status="running",
            cpu_percent=float(parts[2]) if parts[2].replace('.', '').isdigit() else 0.0,
            mem_percent=float(parts[3]) if parts[3].replace('.', '').isdigit() else 0.0
        )

    def detect_crash(self, expected_pid: int, process_name: str) -> bool:
        if self.is_running(expected_pid):
            return False

        remaining = self.get_pid_by_name(process_name)
        crashed = len(remaining) == 0

        if crashed:
            logger.warning("Process %d ('%s') crashed", expected_pid, process_name)

        return crashed

    def detect_zombies(self) -> List[ProcessInfo]:
        result = self.executor.execute(self.target, "ps -eo pid,comm,stat,args 2>/dev/null")
        zombies: List[ProcessInfo] = []

        for line in result.stdout.splitlines()[1:]:
            parts = line.split(None, 3)
            if len(parts) < 3:
                continue
            pid_str, comm, stat_field = parts[0], parts[1], parts[2]
            args = parts[3] if len(parts) > 3 else comm

            if "Z" in stat_field:
                zombies.append(
                    ProcessInfo(
                        pid=int(pid_str),
                        name=comm,
                        command=args,
                        status="zombie"
                    )
                )

        if zombies:
            logger.warning("Found %d zombie processes", len(zombies))

        return zombies

    def detect_duplicates(self, process_name: str) -> List[int]:
        pids = self.get_pid_by_name(process_name)
        if len(pids) > 1:
            logger.warning("Found %d duplicate processes for '%s': %s", len(pids), process_name, pids)
        return pids

    def kill_duplicates(self, process_name: str, keep_oldest: bool = True) -> List[int]:
        pids = sorted(self.get_pid_by_name(process_name))
        if len(pids) <= 1:
            return []

        survivor = pids[0] if keep_oldest else pids[-1]
        to_kill = [p for p in pids if p != survivor]

        for pid in to_kill:
            self.executor.execute(self.target, f"kill -TERM {pid} 2>/dev/null || kill -KILL {pid} 2>/dev/null || true")

        logger.info("Killed %d duplicate processes, kept PID %d", len(to_kill), survivor)
        return to_kill

    def wait_for_stable(self, pid: int, stable_seconds: float = 5.0, poll_interval: float = 0.5) -> bool:
        elapsed = 0.0
        while elapsed < stable_seconds:
            if not self.is_running(pid):
                return False
            time.sleep(poll_interval)
            elapsed += poll_interval
        return True

    def get_service_status(self, service_name: str) -> Dict[str, Any]:
        status: Dict[str, Any] = {"name": service_name, "active": False}

        result = self.executor.execute(self.target, f"systemctl is-active {service_name} 2>/dev/null")
        if result.succeeded:
            status["active"] = result.stdout.strip() == "active"
            status["mode"] = "systemd"

        result = self.executor.execute(self.target, f"systemctl status {service_name} --no-pager 2>/dev/null | head -20")
        if result.succeeded:
            status["status_output"] = result.stdout

        result = self.executor.execute(self.target, f"pgrep -f {service_name} | head -1")
        if result.succeeded and result.stdout.strip().isdigit():
            status["pid"] = int(result.stdout.strip())

        return status

    def get_restart_count(self, service_name: str) -> int:
        result = self.executor.execute(
            self.target,
            f"systemctl show {service_name} --property=NRestarts --value 2>/dev/null"
        )
        if result.succeeded and result.stdout.strip().isdigit():
            return int(result.stdout.strip())
        return 0

    def cleanup_orphans(self, process_name: str, max_age_seconds: float = 3600) -> None:
        pids = self.get_pid_by_name(process_name)
        for pid in pids:
            info = self.get_process_info(pid)
            if info and info.status == "zombie":
                self.executor.execute(self.target, f"kill -KILL {pid} 2>/dev/null || true")
                logger.info("Cleaned up zombie process %d", pid)
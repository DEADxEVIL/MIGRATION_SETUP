import json
import logging
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | migration_agent | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/migration_agent.log") if os.geteuid() == 0
        else logging.FileHandler(str(Path.home() / "migration_agent.log")),
    ]
)
logger = logging.getLogger("migration_agent")


# Constants
DEFAULT_PORT = 9911
DEFAULT_HEARTBEAT_FILE = "/tmp/migration_agent_heartbeat"
DEFAULT_BOOT_MARKER_FILE = "/tmp/migration_agent_boot_id"
HEARTBEAT_INTERVAL = 5.0
DEFAULT_COMMAND_TIMEOUT = 300.0


@dataclass
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ResourceReporter:
    """Reports system resources using only standard library."""
    
    @staticmethod
    def cpu_percent(sample_interval: float = 0.5) -> float:
        try:
            def read_cpu_times():
                with open("/proc/stat") as f:
                    parts = f.readline().split()
                values = list(map(int, parts[1:]))
                idle = values[3] + values[4]
                total = sum(values)
                return idle, total
            
            idle1, total1 = read_cpu_times()
            time.sleep(sample_interval)
            idle2, total2 = read_cpu_times()
            idle_delta = idle2 - idle1
            total_delta = total2 - total1
            
            if total_delta == 0:
                return 0.0
            return round((1.0 - idle_delta / total_delta) * 100.0, 2)
        except (FileNotFoundError, OSError, ValueError, IndexError):
            return -1.0
    
    @staticmethod
    def ram_percent() -> Dict[str, float]:
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    key, value = line.split(":", 1)
                    info[key.strip()] = int(value.strip().split()[0])
            
            total = info.get("MemTotal", 0)
            available = info.get("MemAvailable", 0)
            used = total - available
            percent = round((used / total) * 100.0, 2) if total else -1.0
            
            return {
                "total_mb": round(total / 1024, 1),
                "used_mb": round(used / 1024, 1),
                "available_mb": round(available / 1024, 1),
                "percent_used": percent,
            }
        except (FileNotFoundError, OSError, ValueError, KeyError):
            return {"total_mb": -1, "used_mb": -1, "available_mb": -1, "percent_used": -1}
    
    @staticmethod
    def disk_usage(path: str = "/") -> Dict[str, float]:
        try:
            usage = os.statvfs(path)
            total = usage.f_frsize * usage.f_blocks
            free = usage.f_frsize * usage.f_bavail
            used = total - free
            percent = round((used / total) * 100.0, 2) if total else -1.0
            
            return {
                "total_gb": round(total / (1024 ** 3), 2),
                "used_gb": round(used / (1024 ** 3), 2),
                "free_gb": round(free / (1024 ** 3), 2),
                "percent_used": percent,
            }
        except OSError:
            return {"total_gb": -1, "used_gb": -1, "free_gb": -1, "percent_used": -1}
    
    @staticmethod
    def running_processes(limit: int = 50) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid,ppid,pcpu,pmem,stat,comm", "--sort=-pcpu"],
                capture_output=True, text=True, timeout=10
            )
            lines = result.stdout.strip().splitlines()[1:limit+1]
            processes = []
            for line in lines:
                parts = line.split(None, 5)
                if len(parts) < 6:
                    continue
                processes.append({
                    "pid": int(parts[0]),
                    "ppid": int(parts[1]),
                    "cpu_percent": float(parts[2]),
                    "mem_percent": float(parts[3]),
                    "state": parts[4],
                    "command": parts[5],
                })
            return processes
        except (subprocess.SubprocessError, ValueError, OSError):
            return []
    
    @staticmethod
    def get_boot_id() -> str:
        try:
            with open("/proc/sys/kernel/random/boot_id") as f:
                return f.read().strip()
        except OSError:
            return "unknown"


class CommandExecutor:
    """Executes commands with timeouts."""
    
    def __init__(self, default_timeout: float = DEFAULT_COMMAND_TIMEOUT):
        self.default_timeout = default_timeout
    
    def _run_subprocess(self, args: List[str], timeout: float) -> CommandResult:
        start = time.monotonic()
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout
            )
            return CommandResult(
                stdout=proc.stdout,
                stderr=proc.stderr,
                exit_code=proc.returncode,
                duration_seconds=time.monotonic() - start,
            )
        except subprocess.TimeoutExpired as e:
            return CommandResult(
                stdout=e.stdout or "",
                stderr=f"Command timed out after {timeout}s",
                exit_code=-1,
                duration_seconds=time.monotonic() - start,
            )
        except OSError as e:
            return CommandResult(
                stdout="",
                stderr=f"Failed to execute: {e}",
                exit_code=-1,
                duration_seconds=time.monotonic() - start,
            )
    
    def run_shell(self, command: str, timeout: Optional[float] = None) -> CommandResult:
        return self._run_subprocess(
            ["/bin/bash", "-lc", command],
            timeout or self.default_timeout
        )
    
    def run_python(self, code: str, timeout: Optional[float] = None) -> CommandResult:
        return self._run_subprocess(
            [sys.executable, "-c", code],
            timeout or self.default_timeout
        )
    
    def run_apt(self, args: List[str], timeout: Optional[float] = None) -> CommandResult:
        prefix = [] if os.geteuid() == 0 else ["sudo"]
        cmd = prefix + ["/usr/bin/apt-get", "-y"] + args
        return self._run_subprocess(cmd, timeout or self.default_timeout)
    
    def run_pip(self, args: List[str], timeout: Optional[float] = None) -> CommandResult:
        cmd = [sys.executable, "-m", "pip"] + args
        return self._run_subprocess(cmd, timeout or self.default_timeout)


class MigrationAgent:
    """Multi-threaded TCP server for command execution."""
    
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        heartbeat_file: str = DEFAULT_HEARTBEAT_FILE,
        boot_marker_file: str = DEFAULT_BOOT_MARKER_FILE,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self.heartbeat_file = heartbeat_file
        self.boot_marker_file = boot_marker_file
        self.executor = CommandExecutor(command_timeout)
        self.reporter = ResourceReporter()
        
        self._server_socket: Optional[socket.socket] = None
        self._shutdown_event = threading.Event()
        self._client_threads: List[threading.Thread] = []
        self._lock = threading.RLock()
        self._start_time = time.time()
        self._request_count = 0
    
    def start(self) -> None:
        """Start the agent server."""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind((self.host, self.port))
        self._server_socket.listen(64)
        self._server_socket.settimeout(1.0)
        
        logger.info("MigrationAgent listening on %s:%d", self.host, self.port)
        
        # Start heartbeat thread
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="heartbeat", daemon=True
        )
        heartbeat_thread.start()
        
        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        
        # Main loop
        try:
            while not self._shutdown_event.is_set():
                try:
                    conn, addr = self._server_socket.accept()
                except socket.timeout:
                    continue
                
                thread = threading.Thread(
                    target=self._client_loop, args=(conn, addr), daemon=True
                )
                thread.start()
                with self._lock:
                    self._client_threads.append(thread)
        finally:
            self._shutdown()
    
    def _heartbeat_loop(self) -> None:
        """Background heartbeat writer."""
        while not self._shutdown_event.is_set():
            try:
                # Write heartbeat
                with open(self.heartbeat_file, "w") as f:
                    f.write(str(time.time()))
                
                # Check for reboot
                self._check_reboot()
                
                # Log resources periodically
                if int(time.time()) % 60 == 0:
                    cpu = self.reporter.cpu_percent()
                    ram = self.reporter.ram_percent()
                    logger.debug("Resources: CPU=%s%%, RAM=%s%%", cpu, ram.get("percent_used", -1))
                    
            except OSError as e:
                logger.error("Heartbeat error: %s", e)
            
            self._shutdown_event.wait(HEARTBEAT_INTERVAL)
    
    def _check_reboot(self) -> None:
        """Detect if system has rebooted."""
        current_boot_id = self.reporter.get_boot_id()
        previous_boot_id = None
        
        if os.path.exists(self.boot_marker_file):
            try:
                with open(self.boot_marker_file) as f:
                    previous_boot_id = f.read().strip()
            except OSError:
                pass
        
        if previous_boot_id is not None and previous_boot_id != current_boot_id:
            logger.warning("Reboot detected: %s -> %s", previous_boot_id, current_boot_id)
            self._start_time = time.time()
        
        try:
            with open(self.boot_marker_file, "w") as f:
                f.write(current_boot_id)
        except OSError as e:
            logger.error("Failed to write boot marker: %s", e)
    
    def _client_loop(self, conn: socket.socket, addr: Any) -> None:
        """Handle a client connection."""
        logger.info("Client connected: %s", addr)
        buffer = b""
        
        try:
            conn.settimeout(1.0)
            while not self._shutdown_event.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                
                if not chunk:
                    break
                
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    
                    try:
                        request = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError as e:
                        response = {"stdout": "", "stderr": f"Invalid JSON: {e}", "exit_code": -1}
                    else:
                        response = self._handle_request(request)
                    
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.warning("Client %s disconnected: %s", addr, e)
        finally:
            conn.close()
            logger.info("Client disconnected: %s", addr)
    
    def _handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a request and return response."""
        self._request_count += 1
        req_type = request.get("type")
        timeout = request.get("timeout")
        
        try:
            if req_type == "shell":
                result = self.executor.run_shell(request["command"], timeout)
                return result.to_dict()
            
            elif req_type == "python":
                result = self.executor.run_python(request["code"], timeout)
                return result.to_dict()
            
            elif req_type == "apt":
                result = self.executor.run_apt(request.get("args", []), timeout)
                return result.to_dict()
            
            elif req_type == "pip":
                result = self.executor.run_pip(request.get("args", []), timeout)
                return result.to_dict()
            
            elif req_type == "status":
                return {
                    "uptime_seconds": round(time.time() - self._start_time, 1),
                    "cpu_percent": self.reporter.cpu_percent(),
                    "ram": self.reporter.ram_percent(),
                    "disk": self.reporter.disk_usage(),
                    "boot_id": self.reporter.get_boot_id(),
                    "requests_handled": self._request_count,
                    "connections": len(self._client_threads),
                }
            
            elif req_type == "processes":
                return {
                    "processes": self.reporter.running_processes(request.get("limit", 50))
                }
            
            elif req_type == "health":
                return {
                    "alive": True,
                    "uptime": round(time.time() - self._start_time, 1),
                    "heartbeat_file": self.heartbeat_file,
                    "heartbeat_exists": os.path.exists(self.heartbeat_file),
                    "memory": self.reporter.ram_percent(),
                    "disk": self.reporter.disk_usage(),
                }
            
            elif req_type == "resources":
                return {
                    "cpu_percent": self.reporter.cpu_percent(),
                    "ram": self.reporter.ram_percent(),
                    "disk": self.reporter.disk_usage(),
                    "processes": len(self.reporter.running_processes(1)),
                }
            
            elif req_type == "restart_self":
                threading.Thread(target=self._restart_self, daemon=True).start()
                return {"stdout": "Restart initiated", "stderr": "", "exit_code": 0}
            
            elif req_type == "ping":
                return {"pong": True, "timestamp": time.time(), "uptime": round(time.time() - self._start_time, 1)}
            
            else:
                return {"stdout": "", "stderr": f"Unknown request type: {req_type}", "exit_code": -1}
        
        except KeyError as e:
            return {"stdout": "", "stderr": f"Missing required field: {e}", "exit_code": -1}
        except Exception as e:
            return {
                "stdout": "",
                "stderr": f"Error: {e}\n{traceback.format_exc()}",
                "exit_code": -1,
            }
    
    def _handle_signal(self, signum: int, frame: Any) -> None:
        """Handle termination signals."""
        logger.info("Received signal %d, shutting down", signum)
        self._shutdown_event.set()
    
    def _shutdown(self) -> None:
        """Shut down the agent."""
        if self._server_socket:
            self._server_socket.close()
        
        # Wait for client threads
        for thread in self._client_threads:
            try:
                thread.join(timeout=2.0)
            except Exception:
                pass
        
        logger.info("MigrationAgent shut down")
    
    def _restart_self(self) -> None:
        """Restart the agent process."""
        logger.info("Restarting agent")
        time.sleep(1.0)
        python = sys.executable
        os.execv(python, [python] + sys.argv)


def main() -> None:
    """Entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Migration agent daemon")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--heartbeat-file", default=DEFAULT_HEARTBEAT_FILE)
    parser.add_argument("--boot-marker-file", default=DEFAULT_BOOT_MARKER_FILE)
    parser.add_argument("--command-timeout", type=float, default=DEFAULT_COMMAND_TIMEOUT)
    args = parser.parse_args()
    
    agent = MigrationAgent(
        host=args.host,
        port=args.port,
        heartbeat_file=args.heartbeat_file,
        boot_marker_file=args.boot_marker_file,
        command_timeout=args.command_timeout,
    )
    agent.start()


if __name__ == "__main__":
    main()
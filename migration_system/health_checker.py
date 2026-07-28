from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .command_executor import CommandExecutor, CommandResult

logger = logging.getLogger("health_checker")


@dataclass
class CheckOutcome:
    name: str
    passed: bool
    detail: str
    value: Optional[float] = None
    severity: str = "warning"


@dataclass
class HealthReport:
    checks: List[CheckOutcome] = field(default_factory=list)
    healthy: bool = True
    checked_at: float = field(default_factory=time.time)
    duration_seconds: float = 0.0

    def add(self, outcome: CheckOutcome) -> None:
        self.checks.append(outcome)
        if not outcome.passed and outcome.severity == "critical":
            self.healthy = False

    def to_json(self) -> str:
        return json.dumps({
            "healthy": self.healthy,
            "checked_at": self.checked_at,
            "duration_seconds": round(self.duration_seconds, 2),
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "detail": c.detail,
                    "value": c.value,
                    "severity": c.severity,
                }
                for c in self.checks
            ]
        }, indent=2)

    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.passed)
        total = len(self.checks)
        return f"Health: {passed}/{total} checks passed (healthy: {self.healthy})"


TRACEBACK_PATTERN = re.compile(r"Traceback \(most recent call last\)", re.IGNORECASE)
POLLING_PATTERNS = (
    re.compile(r"start_polling", re.IGNORECASE),
    re.compile(r"polling started", re.IGNORECASE),
    re.compile(r"bot is running", re.IGNORECASE),
    re.compile(r"application started", re.IGNORECASE),
)


class HealthChecker:
    """Runs a battery of health checks using CommandExecutor."""

    def __init__(
        self,
        command_executor: CommandExecutor,
        target: str = "destination",
        cpu_warning_threshold: float = 80.0,
        cpu_critical_threshold: float = 95.0,
        ram_warning_threshold: float = 80.0,
        ram_critical_threshold: float = 95.0,
        restart_loop_threshold: int = 3,
        heartbeat_max_age_seconds: float = 30.0,
    ) -> None:
        self.executor = command_executor
        self.target = target
        self.cpu_warning_threshold = cpu_warning_threshold
        self.cpu_critical_threshold = cpu_critical_threshold
        self.ram_warning_threshold = ram_warning_threshold
        self.ram_critical_threshold = ram_critical_threshold
        self.restart_loop_threshold = restart_loop_threshold
        self.heartbeat_max_age_seconds = heartbeat_max_age_seconds

    def check_process_alive(self, pid: int) -> CheckOutcome:
        result = self.executor.execute(self.target, f"kill -0 {pid} 2>/dev/null")
        return CheckOutcome(
            name="process_alive",
            passed=result.succeeded,
            detail=f"PID {pid} is {'alive' if result.succeeded else 'dead'}",
            severity="critical"
        )

    def check_cpu_usage(self, pid: int) -> CheckOutcome:
        result = self.executor.execute(self.target, f"ps -p {pid} -o %cpu= 2>/dev/null | tr -d ' '")
        if not result.succeeded or not result.stdout.strip():
            return CheckOutcome(
                name="cpu_usage",
                passed=False,
                detail="Could not get CPU usage",
                severity="warning"
            )

        try:
            cpu = float(result.stdout.strip())
        except ValueError:
            return CheckOutcome(
                name="cpu_usage",
                passed=False,
                detail=f"Could not parse CPU: {result.stdout}",
                severity="warning"
            )

        if cpu >= self.cpu_critical_threshold:
            passed = False
            severity = "critical"
            detail = f"CPU is {cpu:.1f}% (critical > {self.cpu_critical_threshold}%)"
        elif cpu >= self.cpu_warning_threshold:
            passed = False
            severity = "warning"
            detail = f"CPU is {cpu:.1f}% (warning > {self.cpu_warning_threshold}%)"
        else:
            passed = True
            severity = "info"
            detail = f"CPU is {cpu:.1f}%"

        return CheckOutcome(
            name="cpu_usage",
            passed=passed,
            detail=detail,
            value=cpu,
            severity=severity
        )

    def check_ram_usage(self, pid: int) -> CheckOutcome:
        result = self.executor.execute(self.target, f"ps -p {pid} -o %mem= 2>/dev/null | tr -d ' '")
        if not result.succeeded or not result.stdout.strip():
            return CheckOutcome(
                name="ram_usage",
                passed=False,
                detail="Could not get RAM usage",
                severity="warning"
            )

        try:
            mem = float(result.stdout.strip())
        except ValueError:
            return CheckOutcome(
                name="ram_usage",
                passed=False,
                detail=f"Could not parse RAM: {result.stdout}",
                severity="warning"
            )

        if mem >= self.ram_critical_threshold:
            passed = False
            severity = "critical"
            detail = f"RAM is {mem:.1f}% (critical > {self.ram_critical_threshold}%)"
        elif mem >= self.ram_warning_threshold:
            passed = False
            severity = "warning"
            detail = f"RAM is {mem:.1f}% (warning > {self.ram_warning_threshold}%)"
        else:
            passed = True
            severity = "info"
            detail = f"RAM is {mem:.1f}%"

        return CheckOutcome(
            name="ram_usage",
            passed=passed,
            detail=detail,
            value=mem,
            severity=severity
        )

    def check_polling_started(self, log_path: str, tail_lines: int = 200) -> CheckOutcome:
        result = self.executor.execute(self.target, f"tail -n {tail_lines} {log_path} 2>/dev/null")
        if not result.succeeded:
            return CheckOutcome(
                name="polling_started",
                passed=False,
                detail="Could not read log file",
                severity="critical"
            )

        matched = any(p.search(result.stdout) for p in POLLING_PATTERNS)
        return CheckOutcome(
            name="polling_started",
            passed=matched,
            detail="Polling start marker found" if matched else "No polling start marker found",
            severity="critical"
        )

    def check_no_traceback(self, log_path: str, tail_lines: int = 500) -> CheckOutcome:
        result = self.executor.execute(self.target, f"tail -n {tail_lines} {log_path} 2>/dev/null")
        if not result.succeeded:
            return CheckOutcome(
                name="no_traceback",
                passed=True,
                detail="Could not read log file, skipping traceback check",
                severity="info"
            )

        found = bool(TRACEBACK_PATTERN.search(result.stdout))
        return CheckOutcome(
            name="no_traceback",
            passed=not found,
            detail="Traceback detected in logs" if found else "No tracebacks found",
            severity="critical" if found else "info"
        )

    def check_no_restart_loop(self, service_name: str) -> CheckOutcome:
        result = self.executor.execute(
            self.target,
            f"systemctl show {service_name} --property=NRestarts --value 2>/dev/null"
        )
        if not result.succeeded or not result.stdout.strip():
            return CheckOutcome(
                name="no_restart_loop",
                passed=True,
                detail="Could not get restart count, skipping",
                severity="info"
            )

        try:
            restarts = int(result.stdout.strip())
        except ValueError:
            return CheckOutcome(
                name="no_restart_loop",
                passed=True,
                detail=f"Could not parse restarts: {result.stdout}",
                severity="info"
            )

        passed = restarts < self.restart_loop_threshold
        return CheckOutcome(
            name="no_restart_loop",
            passed=passed,
            detail=f"Service has restarted {restarts} time(s) (threshold {self.restart_loop_threshold})",
            value=float(restarts),
            severity="critical" if not passed else "info"
        )

    def check_uptime(self, service_name: str) -> CheckOutcome:
        result = self.executor.execute(
            self.target,
            f"systemctl show {service_name} --property=ActiveEnterTimestamp --value 2>/dev/null"
        )
        if not result.succeeded or not result.stdout.strip():
            return CheckOutcome(
                name="uptime",
                passed=True,
                detail="Could not get uptime",
                severity="info"
            )

        return CheckOutcome(
            name="uptime",
            passed=True,
            detail=f"Service active since: {result.stdout.strip()}",
            severity="info"
        )

    def check_heartbeat(self, heartbeat_file: str) -> CheckOutcome:
        result = self.executor.execute(
            self.target,
            f"stat -c %Y {heartbeat_file} 2>/dev/null && date +%s"
        )
        if not result.succeeded:
            return CheckOutcome(
                name="heartbeat",
                passed=False,
                detail="Heartbeat file not found",
                severity="critical"
            )

        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            return CheckOutcome(
                name="heartbeat",
                passed=False,
                detail="Could not read heartbeat timestamps",
                severity="critical"
            )

        try:
            mtime = int(lines[0])
            now = int(lines[1])
        except ValueError:
            return CheckOutcome(
                name="heartbeat",
                passed=False,
                detail="Could not parse heartbeat timestamps",
                severity="critical"
            )

        age = now - mtime
        passed = age <= self.heartbeat_max_age_seconds
        return CheckOutcome(
            name="heartbeat",
            passed=passed,
            detail=f"Last heartbeat {age}s ago (max {self.heartbeat_max_age_seconds}s)",
            value=float(age),
            severity="critical" if not passed else "info"
        )

    def check_network_connectivity(self, target_host: str = "8.8.8.8") -> CheckOutcome:
        result = self.executor.execute(
            self.target,
            f"ping -c 1 -W 3 {target_host} >/dev/null 2>&1"
        )
        return CheckOutcome(
            name="network_connectivity",
            passed=result.succeeded,
            detail=f"Network reachability to {target_host}: {'OK' if result.succeeded else 'FAILED'}",
            severity="critical" if not result.succeeded else "info"
        )

    def check_disk_space(self, path: str = "/", warning_gb: float = 1.0) -> CheckOutcome:
        result = self.executor.execute(
            self.target,
            f"df -BG {path} 2>/dev/null | awk 'NR==2{{print $4}}' | tr -d 'G'"
        )
        if not result.succeeded or not result.stdout.strip():
            return CheckOutcome(
                name="disk_space",
                passed=True,
                detail="Could not check disk space",
                severity="info"
            )

        try:
            free_gb = float(result.stdout.strip())
        except ValueError:
            return CheckOutcome(
                name="disk_space",
                passed=True,
                detail=f"Could not parse disk space: {result.stdout}",
                severity="info"
            )

        passed = free_gb >= warning_gb
        return CheckOutcome(
            name="disk_space",
            passed=passed,
            detail=f"Free disk space: {free_gb:.1f}GB (warning < {warning_gb}GB)",
            value=free_gb,
            severity="warning" if not passed else "info"
        )

    def run_full_check(
        self,
        pid: int,
        service_name: str,
        log_path: str,
        heartbeat_file: Optional[str] = None,
        check_network: bool = True,
        check_disk: bool = True,
    ) -> HealthReport:
        """Run the complete health check battery."""
        start_time = time.time()
        report = HealthReport()

        report.add(self.check_process_alive(pid))
        report.add(self.check_polling_started(log_path))
        report.add(self.check_no_traceback(log_path))
        report.add(self.check_cpu_usage(pid))
        report.add(self.check_ram_usage(pid))
        report.add(self.check_no_restart_loop(service_name))
        report.add(self.check_uptime(service_name))

        if heartbeat_file:
            report.add(self.check_heartbeat(heartbeat_file))

        if check_network:
            report.add(self.check_network_connectivity())

        if check_disk:
            report.add(self.check_disk_space())

        report.duration_seconds = time.time() - start_time

        logger.info(
            "Health check for '%s' completed: %s (%.2fs)",
            service_name,
            report.summary(),
            report.duration_seconds
        )

        return report

    def quick_check(self, pid: int, service_name: str) -> bool:
        """Quick check if service is running and responsive."""
        if not self.check_process_alive(pid).passed:
            return False

        result = self.executor.execute(
            self.target,
            f"systemctl is-active {service_name} 2>/dev/null"
        )
        if result.succeeded and result.stdout.strip() != "active":
            return False

        return True
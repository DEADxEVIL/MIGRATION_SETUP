"""
capability_detector.py
======================

READ-ONLY capability detection using CommandExecutor and CredentialManager.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .command_executor import CommandExecutor, CommandResult
from .credential_manager import CredentialManager

logger = logging.getLogger("capability_detector")


@dataclass
class CapabilityReport:
    """Complete capability report."""
    can_proceed: bool
    issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing_packages: List[str] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    estimated_success_probability: float = 0.0
    estimated_time_seconds: int = 0
    os_version: str = ""
    python_version: str = ""
    disk_free_gb: float = 0.0
    ram_available_mb: float = 0.0
    cpu_cores: int = 0

    def summary(self) -> str:
        status = "✅ PASSED" if self.can_proceed else "❌ FAILED"
        lines = [f"Capability Check: {status}"]
        lines.append(f"  OS: {self.os_version}")
        lines.append(f"  Python: {self.python_version}")
        lines.append(f"  Disk: {self.disk_free_gb:.1f}GB free")
        lines.append(f"  RAM: {self.ram_available_mb:.0f}MB available")
        lines.append(f"  CPU Cores: {self.cpu_cores}")

        if self.issues:
            lines.append(f"  Issues ({len(self.issues)}):")
            for issue in self.issues[:3]:
                lines.append(f"    - {issue}")

        if self.warnings:
            lines.append(f"  Warnings ({len(self.warnings)}):")
            for warning in self.warnings[:3]:
                lines.append(f"    - {warning}")

        lines.append(f"  Success Probability: {self.estimated_success_probability*100:.0f}%")
        lines.append(f"  Estimated Time: {self.estimated_time_seconds}s")

        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)


class CapabilityDetector:
    """
    READ-ONLY capability detector using CommandExecutor and CredentialManager.
    """

    def __init__(self, command_executor: CommandExecutor):
        self.executor = command_executor

    def check_capabilities(
        self,
        host: str,
        username: str = "root",
        password: Optional[str] = None,
        private_key_path: Optional[str] = None,
        port: int = 22,
        credential_name: Optional[str] = None,
    ) -> CapabilityReport:
        """
        Check all capabilities on target VPS.
        
        Uses CredentialManager if credential_name is provided,
        otherwise uses direct credentials.
        """
        report = CapabilityReport(can_proceed=True)

        # Use CredentialManager if credential_name is provided
        if credential_name:
            try:
                credential_manager = CredentialManager()
                creds = credential_manager.get_ssh_credentials(credential_name)
                host = creds.get("host", host)
                username = creds.get("username", username)
                port = creds.get("port", port)
                password = creds.get("password")
                private_key_path = creds.get("private_key_path")
            except Exception as e:
                report.issues.append(f"Failed to load credentials: {e}")
                return report

        target = f"ssh:{host}"
        self.executor.register_ssh_credentials(
            host,
            self._create_credentials(host, username, password, private_key_path, port)
        )

        self._check_os_version(report, target)
        self._check_python_version(report, target)
        self._check_python_packages(report, target)
        self._check_system_packages(report, target)
        self._check_disk_space(report, target)
        self._check_ram(report, target)
        self._check_cpu(report, target)
        self._check_internet(report, target)
        self._check_permissions(report, target)
        self._check_systemd(report, target)
        self._check_screen_tmux(report, target)

        self._calculate_estimates(report)
        report.can_proceed = len(report.issues) == 0

        self.executor.cleanup()

        return report

    def _create_credentials(self, host, username, password, private_key_path, port):
        """Create SSH credentials."""
        from .ssh_manager import SSHCredentials
        return SSHCredentials(
            host=host,
            port=port,
            username=username,
            password=password,
            private_key_path=private_key_path,
        )

    # ... rest of check methods remain the same ...

    def _check_os_version(self, report: CapabilityReport, target: str) -> None:
        result = self.executor.execute(target, "cat /etc/os-release | grep 'PRETTY_NAME' | cut -d= -f2")
        if result.succeeded:
            os_name = result.stdout.strip().strip('"')
            report.os_version = os_name
            report.capabilities["os"] = os_name

            if "Ubuntu" not in os_name:
                report.issues.append(f"OS not Ubuntu: {os_name}")
            else:
                match = re.search(r"(\d+\.\d+)", os_name)
                if match:
                    version = float(match.group(1))
                    if version < 20.04:
                        report.issues.append(f"Ubuntu {version} < 20.04")
        else:
            report.issues.append("Could not determine OS version")

    # ... continue with all other check methods ...

    def _calculate_estimates(self, report: CapabilityReport) -> None:
        prob = 0.95
        prob -= len(report.issues) * 0.2
        prob -= len(report.warnings) * 0.05
        if report.disk_free_gb < 5:
            prob -= 0.1
        if report.ram_available_mb < 512:
            prob -= 0.1
        prob -= len(report.missing_packages) * 0.1
        report.estimated_success_probability = min(max(prob, 0.0), 0.99)
        report.estimated_time_seconds = 60 + (len(report.missing_packages) * 20)
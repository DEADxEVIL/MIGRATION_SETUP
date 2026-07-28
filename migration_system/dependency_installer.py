"""
dependency_installer.py
========================

Installs all dependencies for a Python project using CommandExecutor.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .command_executor import CommandExecutor, CommandResult

logger = logging.getLogger("dependency_installer")


@dataclass
class InstallStepResult:
    name: str
    command: str
    succeeded: bool
    attempts: int
    stdout: str
    stderr: str
    conflict_detected: bool = False
    duration_seconds: float = 0.0


@dataclass
class InstallationReport:
    steps: List[InstallStepResult] = field(default_factory=list)
    overall_success: bool = True
    total_duration: float = 0.0

    def to_json(self) -> str:
        return json.dumps({
            "overall_success": self.overall_success,
            "total_duration": round(self.total_duration, 2),
            "steps": [
                {
                    "name": s.name,
                    "command": s.command,
                    "succeeded": s.succeeded,
                    "attempts": s.attempts,
                    "conflict_detected": s.conflict_detected,
                    "duration": round(s.duration_seconds, 2),
                    "stdout": s.stdout[-2000:] if s.stdout else "",
                    "stderr": s.stderr[-2000:] if s.stderr else "",
                }
                for s in self.steps
            ]
        }, indent=2)


SYSTEM_PACKAGES: Dict[str, str] = {
    "python3": "python3",
    "python3-venv": "python3-venv",
    "python3-pip": "python3-pip",
    "python3-dev": "python3-dev",
    "build-essential": "build-essential",
    "gcc": "gcc",
    "g++": "g++",
    "cmake": "cmake",
    "git": "git",
    "ffmpeg": "ffmpeg",
    "curl": "curl",
    "wget": "wget",
    "zip": "zip",
    "unzip": "unzip",
    "libssl-dev": "libssl-dev",
    "libffi-dev": "libffi-dev",
    "ca-certificates": "ca-certificates",
    "sudo": "sudo",
}

CONFLICT_PATTERNS = (
    re.compile(r"resolutionimpossible", re.IGNORECASE),
    re.compile(r"conflicting dependencies", re.IGNORECASE),
    re.compile(r"has requirement .* but you have", re.IGNORECASE),
    re.compile(r"incompatible", re.IGNORECASE),
    re.compile(r"you have held broken packages", re.IGNORECASE),
    re.compile(r"unmet dependencies", re.IGNORECASE),
)


class DependencyInstallerError(Exception):
    """Raised when dependency installation fails."""


class DependencyInstaller:
    """Orchestrates dependency provisioning using CommandExecutor."""

    def __init__(
        self,
        command_executor: CommandExecutor,
        target: str = "destination",
        max_retries: int = 3,
        retry_backoff_base: float = 2.0,
        use_sudo_for_system_packages: bool = True,
    ) -> None:
        self.executor = command_executor
        self.target = target
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base
        self.use_sudo_for_system_packages = use_sudo_for_system_packages

    def _run_with_retry(self, name: str, command: str) -> InstallStepResult:
        last_result: Optional[CommandResult] = None
        conflict_detected = False
        start_time = time.time()

        for attempt in range(1, self.max_retries + 1):
            logger.info("Running step '%s' (attempt %d/%d): %s", name, attempt, self.max_retries, command)
            result = self.executor.execute(self.target, command)
            last_result = result

            combined = f"{result.stdout}\n{result.stderr}"
            if any(p.search(combined) for p in CONFLICT_PATTERNS):
                conflict_detected = True
                logger.warning("Conflict pattern detected in step '%s'", name)

            if result.succeeded:
                return InstallStepResult(
                    name=name,
                    command=command,
                    succeeded=True,
                    attempts=attempt,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    conflict_detected=conflict_detected,
                    duration_seconds=time.time() - start_time,
                )

            logger.warning(
                "Step '%s' failed on attempt %d/%d (exit=%d). Retrying after backoff.",
                name, attempt, self.max_retries, result.exit_code
            )
            time.sleep(self.retry_backoff_base ** attempt)

        assert last_result is not None
        return InstallStepResult(
            name=name,
            command=command,
            succeeded=False,
            attempts=self.max_retries,
            stdout=last_result.stdout,
            stderr=last_result.stderr,
            conflict_detected=conflict_detected,
            duration_seconds=time.time() - start_time,
        )

    def detect_missing_system_packages(self, packages: List[str]) -> List[str]:
        missing: List[str] = []
        for pkg in packages:
            result = self.executor.execute(
                self.target,
                f"dpkg -s {pkg} 2>/dev/null | grep -q 'Status: install ok installed'"
            )
            if not result.succeeded:
                missing.append(pkg)
        return missing

    def install_system_packages(self, packages: Optional[List[str]] = None, update_cache: bool = True) -> InstallationReport:
        target_packages = packages or list(SYSTEM_PACKAGES.values())
        report = InstallationReport()
        sudo_prefix = "sudo " if self.use_sudo_for_system_packages else ""

        if update_cache:
            update_result = self._run_with_retry(
                "apt_update", f"{sudo_prefix}apt-get update -y"
            )
            report.steps.append(update_result)
            if not update_result.succeeded:
                report.overall_success = False
                logger.error("APT update failed, continuing with installation attempt")

        missing = self.detect_missing_system_packages(target_packages)
        if not missing:
            logger.info("All requested system packages are already installed.")
            return report

        install_cmd = f"{sudo_prefix}apt-get install -y " + " ".join(missing)
        install_result = self._run_with_retry("apt_install_system_packages", install_cmd)
        report.steps.append(install_result)
        if not install_result.succeeded:
            report.overall_success = False

        return report

    def ensure_venv(self, project_dir: str, venv_name: str = ".venv", python_bin: str = "python3") -> InstallStepResult:
        venv_path = f"{project_dir}/{venv_name}"
        check = self.executor.execute(self.target, f"test -d {venv_path}")
        if check.succeeded:
            logger.info("Virtual environment already exists at %s", venv_path)
            return InstallStepResult(
                name="ensure_venv",
                command="(skipped, already exists)",
                succeeded=True,
                attempts=0,
                stdout="already exists",
                stderr="",
                duration_seconds=0.0,
            )

        return self._run_with_retry(
            "create_venv",
            f"cd {project_dir} && {python_bin} -m venv {venv_name}"
        )

    def upgrade_pip_toolchain(self, project_dir: str, venv_name: str = ".venv") -> InstallStepResult:
        pip_bin = f"{project_dir}/{venv_name}/bin/pip"
        return self._run_with_retry(
            "upgrade_pip_toolchain",
            f"{pip_bin} install --upgrade pip setuptools wheel"
        )

    def install_requirements(
        self,
        project_dir: str,
        venv_name: str = ".venv",
        requirements_files: Tuple[str, ...] = ("requirements.txt",),
        extra_index_url: Optional[str] = None
    ) -> InstallationReport:
        report = InstallationReport()
        pip_bin = f"{project_dir}/{venv_name}/bin/pip"
        index_url = f"--index-url {extra_index_url}" if extra_index_url else ""

        for req_file in requirements_files:
            exists = self.executor.execute(self.target, f"test -f {project_dir}/{req_file}")
            if not exists.succeeded:
                logger.info("Requirements file '%s' not found, skipping", req_file)
                continue

            step_result = self._run_with_retry(
                f"install_{req_file}",
                f"{pip_bin} install {index_url} -r {project_dir}/{req_file}"
            )
            report.steps.append(step_result)
            if not step_result.succeeded:
                report.overall_success = False

        return report

    def install_package(self, project_dir: str, venv_name: str = ".venv", package: str = "", extra_args: str = "") -> InstallStepResult:
        pip_bin = f"{project_dir}/{venv_name}/bin/pip"
        return self._run_with_retry(
            f"install_{package}",
            f"{pip_bin} install {package} {extra_args}"
        )

    def install_all(
        self,
        project_dir: str,
        venv_name: str = ".venv",
        python_bin: str = "python3",
        requirements_files: Tuple[str, ...] = ("requirements.txt",),
        system_packages: Optional[List[str]] = None,
        extra_packages: Optional[List[str]] = None,
        extra_index_url: Optional[str] = None,
    ) -> InstallationReport:
        start_time = time.time()
        combined = InstallationReport()

        sys_report = self.install_system_packages(system_packages)
        combined.steps.extend(sys_report.steps)
        if not sys_report.overall_success:
            combined.overall_success = False
            logger.warning("System package installation had issues, continuing...")

        venv_step = self.ensure_venv(project_dir, venv_name, python_bin)
        combined.steps.append(venv_step)
        if not venv_step.succeeded:
            combined.overall_success = False
            logger.error("Virtual environment creation failed, aborting installation")
            combined.total_duration = time.time() - start_time
            return combined

        pip_upgrade = self.upgrade_pip_toolchain(project_dir, venv_name)
        combined.steps.append(pip_upgrade)
        if not pip_upgrade.succeeded:
            combined.overall_success = False
            logger.warning("Pip toolchain upgrade failed, continuing...")

        req_report = self.install_requirements(project_dir, venv_name, requirements_files, extra_index_url)
        combined.steps.extend(req_report.steps)
        if not req_report.overall_success:
            combined.overall_success = False

        if extra_packages:
            for pkg in extra_packages:
                pkg_result = self.install_package(project_dir, venv_name, pkg)
                combined.steps.append(pkg_result)
                if not pkg_result.succeeded:
                    combined.overall_success = False

        combined.total_duration = time.time() - start_time

        logger.info(
            "Dependency installation complete for '%s': overall_success=%s (%d steps, %.2fs)",
            project_dir,
            combined.overall_success,
            len(combined.steps),
            combined.total_duration
        )
        return combined

    def verify_installation(self, project_dir: str, venv_name: str = ".venv", requirements_files: Tuple[str, ...] = ("requirements.txt",)) -> Dict[str, Any]:
        verification = {
            "venv_exists": False,
            "pip_available": False,
            "requirements_installed": [],
            "missing_packages": [],
            "all_verified": False
        }

        venv_path = f"{project_dir}/{venv_name}"
        result = self.executor.execute(self.target, f"test -d {venv_path}")
        verification["venv_exists"] = result.succeeded

        if not verification["venv_exists"]:
            return verification

        pip_bin = f"{venv_path}/bin/pip"
        result = self.executor.execute(self.target, f"{pip_bin} --version")
        verification["pip_available"] = result.succeeded

        if not verification["pip_available"]:
            return verification

        for req_file in requirements_files:
            req_path = f"{project_dir}/{req_file}"
            exists = self.executor.execute(self.target, f"test -f {req_path}")
            if not exists.succeeded:
                continue

            result = self.executor.execute(
                self.target,
                f"cat {req_path} | grep -v '^#' | grep -v '^$' | cut -d'=' -f1 | cut -d'>' -f1 | cut -d'<' -f1"
            )
            if result.succeeded:
                packages = [p.strip() for p in result.stdout.splitlines() if p.strip()]
                for pkg in packages:
                    check = self.executor.execute(self.target, f"{pip_bin} show {pkg} 2>/dev/null")
                    verification["requirements_installed"].append({
                        "package": pkg,
                        "installed": check.succeeded
                    })
                    if not check.succeeded:
                        verification["missing_packages"].append(pkg)

        verification["all_verified"] = (
            verification["venv_exists"] and
            verification["pip_available"] and
            len(verification["missing_packages"]) == 0
        )

        return verification
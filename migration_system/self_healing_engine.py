from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from diagnosis_engine import Diagnosis, FailureContext, diagnose
from error_pattern_library import best_match

logger = logging.getLogger("self_healing_engine")


@dataclass
class ExecOutcome:
    """Result of executing a command."""
    command: str
    stdout: str
    stderr: str
    exit_code: int
    duration_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


Executor = Callable[[str], ExecOutcome]


@dataclass
class HealingAttempt:
    """A single healing attempt."""
    fix_command: str
    outcome: Optional[ExecOutcome] = None
    applied: bool = False
    reason_skipped: str = ""
    duration_seconds: float = 0.0


@dataclass
class HealingResult:
    """Complete healing result."""
    step: str
    original_command: str
    diagnosis: Diagnosis
    attempts: List[HealingAttempt] = field(default_factory=list)
    retry_outcome: Optional[ExecOutcome] = None
    final_status: str = "unresolved"  # resolved, unresolved, skipped_unsafe
    total_duration_seconds: float = 0.0
    error_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step": self.step,
            "original_command": self.original_command,
            "error_hash": self.error_hash,
            "diagnosis": self.diagnosis.to_dict(),
            "attempts": [
                {
                    "fix_command": a.fix_command,
                    "applied": a.applied,
                    "reason_skipped": a.reason_skipped,
                    "succeeded": a.outcome.succeeded if a.outcome else None,
                    "duration": round(a.duration_seconds, 2),
                }
                for a in self.attempts
            ],
            "retry_outcome": {
                "succeeded": self.retry_outcome.succeeded if self.retry_outcome else None,
                "exit_code": self.retry_outcome.exit_code if self.retry_outcome else None,
                "duration": round(self.retry_outcome.duration_seconds, 2) if self.retry_outcome else None,
            } if self.retry_outcome else None,
            "final_status": self.final_status,
            "total_duration_seconds": round(self.total_duration_seconds, 2),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def compute_error_hash(
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int,
    step: str,
    os_version: str = "",
    python_version: str = "",
) -> str:
    """Compute a unique hash for an error."""
    content = f"{step}|{command}|{exit_code}|{stderr[:500]}|{stdout[:500]}|{os_version}|{python_version}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class SelfHealingEngine:
    """
    Self-healing orchestrator with history tracking.
    One instance per migration run.
    """

    def __init__(
        self,
        executor: Executor,
        max_fix_attempts: int = 3,
        min_confidence_to_auto_fix: float = 0.6,
        allow_risky_fixes: bool = False,
    ) -> None:
        self.executor = executor
        self.max_fix_attempts = max_fix_attempts
        self.min_confidence_to_auto_fix = min_confidence_to_auto_fix
        self.allow_risky_fixes = allow_risky_fixes

        # History tracking: (step, command) -> set of attempted fix commands
        self._fix_history: Dict[Tuple[str, str], set] = {}
        self._history_log: List[Dict[str, Any]] = []
        self._successful_fixes: Dict[str, int] = {}  # error_hash -> success_count

    def _history_key(self, step: str, command: str) -> Tuple[str, str]:
        return (step, command)

    def _already_attempted(self, step: str, command: str, fix: str) -> bool:
        return fix in self._fix_history.get(self._history_key(step, command), set())

    def _mark_attempted(self, step: str, command: str, fix: str) -> None:
        key = self._history_key(step, command)
        self._fix_history.setdefault(key, set()).add(fix)

    def heal(
        self,
        command: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        current_step: str,
        os_version: str = "",
        python_version: str = "",
        project_metadata: Optional[Dict[str, Any]] = None,
        error_hash: Optional[str] = None,
    ) -> HealingResult:
        """
        Analyze a failure and attempt to automatically resolve it.
        Returns a full structured result regardless of outcome.
        """
        start_time = time.time()

        # Compute error hash
        if error_hash is None:
            error_hash = compute_error_hash(
                command, stdout, stderr, exit_code,
                current_step, os_version, python_version
            )

        # Create context for diagnosis
        context = FailureContext(
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            current_step=current_step,
            os_version=os_version,
            python_version=python_version,
            project_metadata=project_metadata or {},
        )

        # Get diagnosis
        diagnosis = diagnose(context)

        result = HealingResult(
            step=current_step,
            original_command=command,
            diagnosis=diagnosis,
            error_hash=error_hash,
        )

        # Check if we should attempt auto-fix
        risky = diagnosis.risk_level in ("high", "critical")
        if diagnosis.confidence < self.min_confidence_to_auto_fix:
            result.final_status = "skipped_unsafe"
            logger.warning(
                "Skipping auto-fix: confidence=%.2f < threshold=%.2f",
                diagnosis.confidence, self.min_confidence_to_auto_fix
            )
        elif risky and not self.allow_risky_fixes:
            result.final_status = "skipped_unsafe"
            logger.warning(
                "Skipping auto-fix: risk_level=%s (risky fixes disabled)",
                diagnosis.risk_level
            )
        else:
            # Attempt fixes
            applied_any = False
            for fix_command in diagnosis.possible_fixes[:self.max_fix_attempts]:
                # Skip advisory-only fixes
                if fix_command.lower().startswith(("verify", "consider", "inspect", "configure", "manual")):
                    result.attempts.append(HealingAttempt(
                        fix_command=fix_command,
                        applied=False,
                        reason_skipped="Advisory fix, requires human action",
                    ))
                    continue

                # Skip already attempted fixes
                if self._already_attempted(current_step, command, fix_command):
                    result.attempts.append(HealingAttempt(
                        fix_command=fix_command,
                        applied=False,
                        reason_skipped="Already attempted previously",
                    ))
                    continue

                # Apply the fix
                logger.info("Applying fix: %s", fix_command)
                self._mark_attempted(current_step, command, fix_command)

                fix_start = time.time()
                try:
                    outcome = self.executor(fix_command)
                except Exception as e:
                    outcome = ExecOutcome(
                        command=fix_command,
                        stdout="",
                        stderr=str(e),
                        exit_code=-1,
                    )

                attempt = HealingAttempt(
                    fix_command=fix_command,
                    outcome=outcome,
                    applied=True,
                    duration_seconds=time.time() - fix_start,
                )
                result.attempts.append(attempt)

                if outcome.succeeded:
                    applied_any = True
                    # Track successful fix
                    self._successful_fixes[error_hash] = self._successful_fixes.get(error_hash, 0) + 1
                    break

            # Retry original command if recommended
            if diagnosis.retry_recommended:
                logger.info("Retrying original command after fixes")
                retry_start = time.time()
                try:
                    retry_outcome = self.executor(command)
                except Exception as e:
                    retry_outcome = ExecOutcome(
                        command=command,
                        stdout="",
                        stderr=str(e),
                        exit_code=-1,
                    )
                retry_outcome.duration_seconds = time.time() - retry_start
                result.retry_outcome = retry_outcome

                if retry_outcome.succeeded:
                    result.final_status = "resolved"
                    # Track successful fix
                    self._successful_fixes[error_hash] = self._successful_fixes.get(error_hash, 0) + 1
                else:
                    result.final_status = "unresolved"
            else:
                result.final_status = "resolved" if applied_any else "unresolved"

        result.total_duration_seconds = time.time() - start_time

        # Log result
        logger.info(
            "Self-healing for step '%s' completed: status=%s in %.2fs",
            current_step, result.final_status, result.total_duration_seconds
        )

        # Store in history
        self._history_log.append(result.to_dict())

        return result

    def get_history(self) -> List[Dict[str, Any]]:
        """Get full healing history."""
        return list(self._history_log)

    def get_successful_fixes(self) -> Dict[str, int]:
        """Get successful fix counts by error hash."""
        return dict(self._successful_fixes)

    def reset_history(self) -> None:
        """Reset healing history."""
        self._fix_history.clear()
        self._history_log.clear()
        self._successful_fixes.clear()

    def can_auto_heal(self, diagnosis: Diagnosis) -> bool:
        """Check if auto-healing is possible and safe."""
        if diagnosis.confidence < self.min_confidence_to_auto_fix:
            return False
        if diagnosis.risk_level in ("high", "critical") and not self.allow_risky_fixes:
            return False
        if not diagnosis.possible_fixes:
            return False
        return True


if __name__ == "__main__":
    from logger_service import get_logger

    logger = get_logger("test")

    # Create executor that runs commands locally
    import subprocess
    def local_executor(cmd: str) -> ExecOutcome:
        start = time.time()
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        return ExecOutcome(
            command=cmd,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            duration_seconds=time.time() - start,
        )

    healer = SelfHealingEngine(
        local_executor,
        max_fix_attempts=3,
        min_confidence_to_auto_fix=0.5,
        allow_risky_fixes=False,
    )

    # Test with a failure
    result = healer.heal(
        command="python3 -c 'import nonexistent_module'",
        stdout="",
        stderr="ModuleNotFoundError: No module named 'nonexistent_module'",
        exit_code=1,
        current_step="test_import",
    )

    print(result.to_json())
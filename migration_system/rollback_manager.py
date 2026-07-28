"""
rollback_manager.py
===================

Handles undoing a migration on the destination VPS.
NEVER touches the source VPS.

Uses CommandExecutor for ALL command execution.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .state_manager import StateManager, StepStatus
from .command_executor import CommandExecutor, CommandResult

logger = logging.getLogger("rollback_manager")


@dataclass
class RollbackAction:
    """A single rollback action."""
    description: str
    command: str
    succeeded: bool
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0


@dataclass
class RollbackReport:
    """Complete rollback report."""
    migration_id: str
    rollback_type: str  # "complete", "partial", "step"
    actions: List[RollbackAction] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    success: bool = True
    notes: str = ""
    affected_steps: int = 0

    def to_json(self) -> str:
        return json.dumps({
            "migration_id": self.migration_id,
            "rollback_type": self.rollback_type,
            "success": self.success,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round((self.finished_at or time.time()) - self.started_at, 2),
            "affected_steps": self.affected_steps,
            "notes": self.notes,
            "actions": [
                {
                    "description": a.description,
                    "command": a.command,
                    "succeeded": a.succeeded,
                    "duration": round(a.duration_seconds, 2),
                    "stdout": a.stdout[-500:] if a.stdout else "",
                    "stderr": a.stderr[-500:] if a.stderr else "",
                }
                for a in self.actions
            ]
        }, indent=2)


class RollbackManagerError(Exception):
    """Raised for rollback errors."""


class RollbackManager:
    """
    Executes rollback operations on the destination VPS only.
    Uses CommandExecutor for ALL command execution.
    Thread-safe.
    """

    def __init__(
        self,
        command_executor: CommandExecutor,
        state_manager: StateManager,
        bot_service_name: str = "migrated-bot",
        project_directory: str = "/opt/migrated-project",
        target: str = "destination",
    ) -> None:
        self.executor = command_executor
        self.state_manager = state_manager
        self.bot_service_name = bot_service_name
        self.project_directory = project_directory
        self.target = target

    def _run_action(self, description: str, command: str) -> RollbackAction:
        """Execute a rollback action using CommandExecutor."""
        logger.info("Rollback action: %s -> %s", description, command)
        start = time.time()
        result = self.executor.execute(self.target, command, use_sudo=True)
        return RollbackAction(
            description=description,
            command=command,
            succeeded=result.succeeded,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_seconds=time.time() - start,
        )

    def stop_service(self) -> RollbackAction:
        """Stop the bot service."""
        return self._run_action(
            "Stop bot service",
            f"systemctl stop {self.bot_service_name} 2>/dev/null || true"
        )

    def disable_service(self) -> RollbackAction:
        """Disable the bot service from starting on boot."""
        return self._run_action(
            "Disable bot service",
            f"systemctl disable {self.bot_service_name} 2>/dev/null || true"
        )

    def remove_service_unit(self) -> RollbackAction:
        """Remove the systemd unit file."""
        unit_path = f"/etc/systemd/system/{self.bot_service_name}.service"
        return self._run_action(
            "Remove systemd unit",
            f"rm -f {unit_path} && systemctl daemon-reload 2>/dev/null || true"
        )

    def delete_project(self) -> RollbackAction:
        """Delete the project directory."""
        return self._run_action(
            "Delete project directory",
            f"rm -rf {self.project_directory}"
        )

    def delete_venv(self) -> RollbackAction:
        """Delete the virtual environment."""
        return self._run_action(
            "Delete virtual environment",
            f"rm -rf {self.project_directory}/.venv"
        )

    def kill_processes(self, process_name: str) -> RollbackAction:
        """Kill processes matching the name."""
        return self._run_action(
            f"Kill {process_name} processes",
            f"pkill -f {process_name} 2>/dev/null || true"
        )

    def complete_rollback(self, migration_id: str) -> RollbackReport:
        """Full rollback: stop service, remove unit, delete everything."""
        report = RollbackReport(
            migration_id=migration_id,
            rollback_type="complete"
        )

        report.actions.append(self.stop_service())
        report.actions.append(self.disable_service())
        report.actions.append(self.remove_service_unit())
        report.actions.append(self.kill_processes(self.bot_service_name))
        report.actions.append(self.delete_project())

        report.success = all(a.succeeded for a in report.actions)

        steps = self.state_manager.get_steps(migration_id)
        for step in steps:
            if step.step_id:
                self.state_manager.update_step(
                    step.step_id,
                    StepStatus.ROLLED_BACK,
                    error_message="Complete rollback"
                )

        report.affected_steps = len(steps)
        report.finished_at = time.time()
        report.notes = f"Complete rollback: {len(steps)} steps affected"

        logger.info("Complete rollback finished for %s: success=%s",
                   migration_id, report.success)
        return report

    def partial_rollback(self, migration_id: str, checkpoint_step_name: str) -> RollbackReport:
        """Rollback steps after a checkpoint, keeping earlier steps."""
        report = RollbackReport(
            migration_id=migration_id,
            rollback_type="partial"
        )

        all_steps = self.state_manager.get_steps(migration_id)
        checkpoint_index = None
        for i, step in enumerate(all_steps):
            if step.step_name == checkpoint_step_name:
                checkpoint_index = i
                break

        if checkpoint_index is None:
            raise RollbackManagerError(f"Checkpoint '{checkpoint_step_name}' not found")

        steps_to_rollback = all_steps[checkpoint_index + 1:]

        if not steps_to_rollback:
            report.notes = "No steps after checkpoint to rollback"
            report.finished_at = time.time()
            report.success = True
            return report

        report.actions.append(self.stop_service())

        for step in reversed(steps_to_rollback):
            action = self._rollback_step_action(step)
            report.actions.append(action)
            if action.succeeded and step.step_id:
                self.state_manager.update_step(
                    step.step_id,
                    StepStatus.ROLLED_BACK,
                    error_message=f"Partial rollback to checkpoint '{checkpoint_step_name}'"
                )

        report.actions.append(self._run_action(
            "Restart service after partial rollback",
            f"systemctl start {self.bot_service_name} 2>/dev/null || true"
        ))

        report.success = all(a.succeeded for a in report.actions)
        report.affected_steps = len(steps_to_rollback)
        report.finished_at = time.time()
        report.notes = f"Partial rollback: {len(steps_to_rollback)} steps rolled back"

        return report

    def _rollback_step_action(self, step: Any) -> RollbackAction:
        """Create a rollback action for a specific step."""
        rollback_commands = {
            "install_deps": f"rm -rf {self.project_directory}/.venv",
            "upload_project": f"rm -rf {self.project_directory}",
            "create_venv": f"rm -rf {self.project_directory}/.venv",
            "pip_install": f"rm -rf {self.project_directory}/.venv",
        }

        command = rollback_commands.get(step.step_name, f"echo 'No rollback for {step.step_name}'")
        return self._run_action(f"Rollback step: {step.step_name}", command)

    def cleanup_destination(self, migration_id: str) -> RollbackReport:
        """Clean up destination VPS without affecting source."""
        report = RollbackReport(
            migration_id=migration_id,
            rollback_type="cleanup"
        )

        report.actions.append(self.stop_service())
        report.actions.append(self.disable_service())
        report.actions.append(self.remove_service_unit())
        report.actions.append(self.kill_processes(self.bot_service_name))
        report.actions.append(self.delete_project())

        report.success = all(a.succeeded for a in report.actions)
        report.finished_at = time.time()
        report.notes = "Destination VPS cleaned up"

        return report
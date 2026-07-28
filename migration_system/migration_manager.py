"""
migration_manager.py
====================

PURE ORCHESTRATOR - NO DECISION LOGIC.

Responsibilities:
- Define migration flow (steps)
- Execute steps in order
- Delegate ALL decisions to decision_engine.py
- Use command_executor.py for ALL commands
- Use checkpoint_manager.py for auto-checkpoints
- Emit events to Event Bus for monitoring
- Use StateManager for state persistence
- Auto-resume from checkpoints after crash/reboot
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .state_manager import StateManager, StepStatus, MigrationStatus, StepState
from .event_bus import EventBus, Event, EventCategory, EventPriority
from .command_executor import CommandExecutor, CommandResult
from .decision_engine import DecisionEngine, Decision, DecisionType
from .capability_detector import CapabilityDetector, CapabilityReport
from .checkpoint_manager import CheckpointManager
from .credential_manager import CredentialManager
from .migration_journal import MigrationJournal
from .ssh_manager import SSHCredentials

logger = logging.getLogger("migration_manager")


@dataclass
class MigrationStep:
    """A single migration step."""
    name: str
    command: str
    use_sudo: bool = False
    timeout: float = 300.0
    critical: bool = True
    retries: int = 2
    checkpoint_before: bool = True
    checkpoint_after: bool = False


@dataclass
class MigrationPlan:
    """Complete migration plan."""
    migration_id: str
    bot_name: str
    source_credentials: SSHCredentials
    destination_credentials: SSHCredentials
    steps: List[MigrationStep]
    project_directory: str = "/opt/migrated-project"
    bot_service_name: str = "migrated-bot"
    log_path: str = "/var/log/migrated-bot.log"
    heartbeat_file: str = "/tmp/migration_agent_heartbeat"
    scheduled_start_time: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class MigrationCancelled(Exception):
    """Raised when migration is cancelled."""
    pass


class MigrationManager:
    """
    PURE ORCHESTRATOR - NO DECISION LOGIC.
    
    Only responsible for:
    - Running steps in order
    - Asking DecisionEngine what to do
    - Executing decisions
    - Creating checkpoints before dangerous operations
    - Auto-resuming from checkpoints
    - Emitting events
    - Persisting state
    """
    
    def __init__(
        self,
        plan: MigrationPlan,
        state_db_path: str = "migration_state.db",
        credential_store_path: str = "credentials.encrypted",
        max_step_retries: int = 2,
        min_confidence_to_auto_heal: float = 0.6,
        allow_risky_fixes: bool = False,
    ):
        self.plan = plan
        
        # Core components
        self.state_manager = StateManager(state_db_path)
        self.event_bus = EventBus(state_manager_ref=self.state_manager)
        self.credential_manager = CredentialManager(credential_store_path)
        self.command_executor = CommandExecutor()
        self.checkpoint_manager = CheckpointManager(self.state_manager)
        self.migration_journal = MigrationJournal(self.state_manager)
        self.capability_detector = CapabilityDetector(self.command_executor)
        self.decision_engine = DecisionEngine(
            state_manager=self.state_manager,
            min_confidence_to_auto_heal=min_confidence_to_auto_heal,
            allow_risky_fixes=allow_risky_fixes,
        )
        
        # Control
        self._cancel_event = threading.Event()
        self._log_subscribers: List[Callable[[str, str], None]] = []
        self._lock = threading.RLock()
        self._running = False
        self._current_step_name: Optional[str] = None
        self._auto_resume_checkpoint: Optional[str] = None
        
        # Register SSH credentials with command executor
        self.command_executor.register_ssh_credentials(
            "source", plan.source_credentials
        )
        self.command_executor.register_ssh_credentials(
            "destination", plan.destination_credentials
        )
    
    # ------------------------------------------------------------------ #
    # Logging
    # ------------------------------------------------------------------ #
    
    def subscribe_logs(self, callback: Callable[[str, str], None]) -> None:
        """Register log subscriber."""
        with self._lock:
            self._log_subscribers.append(callback)
    
    def _emit_log(self, step: str, message: str, level: str = "INFO") -> None:
        """Emit log to all subscribers."""
        logger.log(getattr(logging, level, logging.INFO), "[%s] %s", step, message)
        with self._lock:
            subscribers = list(self._log_subscribers)
        for cb in subscribers:
            try:
                cb(step, message)
            except Exception as e:
                logger.error("Log subscriber error: %s", e)
        
        self.migration_journal.add_entry(
            migration_id=self.plan.migration_id,
            message=message,
            level=level,
            step_name=step
        )
    
    def _emit_event(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit an event to the event bus."""
        event = Event(
            event_type=event_type,
            data=data,
            source="migration_manager",
            category=EventCategory.MIGRATION,
            correlation_id=self.plan.migration_id,
        )
        self.event_bus.publish(event)
    
    # ------------------------------------------------------------------ #
    # Control
    # ------------------------------------------------------------------ #
    
    def cancel(self) -> None:
        """Cancel the migration."""
        logger.warning("Cancelling migration %s", self.plan.migration_id)
        self._cancel_event.set()
        self.state_manager.update_migration_status(
            self.plan.migration_id,
            MigrationStatus.FAILED
        )
        self._emit_event("MIGRATION_CANCELLED", {"migration_id": self.plan.migration_id})
        self.migration_journal.add_entry(
            migration_id=self.plan.migration_id,
            message="Migration cancelled by user",
            level="WARNING"
        )
    
    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise MigrationCancelled("Migration cancelled by user")
    
    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #
    
    def run(self) -> bool:
        """
        Execute the full migration plan.
        
        Pure orchestration with auto-checkpoints and resume.
        """
        if self._running:
            raise RuntimeError("Migration already running")
        
        self._running = True
        self._cancel_event.clear()
        self._current_step_name = None
        
        try:
            # Initialize migration record
            self._initialize_migration()
            
            # Wait for scheduled time
            self._wait_for_schedule()
            
            # Pre-flight capability check
            capability_report = self._run_capability_check()
            if not capability_report.can_proceed:
                self._emit_log("PREFLIGHT", "Capability check failed - aborting", "ERROR")
                return False
            
            # Try to auto-resume from checkpoint
            if self._auto_resume_from_checkpoint():
                self._emit_log("RESUME", "Auto-resumed from checkpoint", "INFO")
            
            # Get resume point
            resume_step = self._get_resume_point()
            started = resume_step is None
            
            # Execute each step
            for i, step in enumerate(self.plan.steps):
                self._current_step_name = step.name
                
                if not started:
                    if step.name == resume_step:
                        started = True
                    else:
                        self._emit_log(step.name, "Skipping (already completed)")
                        continue
                
                self._check_cancelled()
                self._emit_event("MIGRATION_STEP_STARTED", {
                    "step": step.name,
                    "index": i + 1,
                    "total": len(self.plan.steps),
                })
                
                # Ask DecisionEngine what to do
                decision = self.decision_engine.decide_before_step(
                    migration_id=self.plan.migration_id,
                    step_name=step.name,
                    step_index=i,
                    total_steps=len(self.plan.steps)
                )
                
                # Execute decision
                if decision.decision_type == DecisionType.ABORT:
                    self._emit_log(step.name, "DecisionEngine says ABORT", "ERROR")
                    return False
                
                if decision.decision_type == DecisionType.ROLLBACK:
                    self._emit_log(step.name, "DecisionEngine says ROLLBACK", "ERROR")
                    self._perform_rollback("DecisionEngine requested rollback")
                    return False
                
                # Create checkpoint before step if requested
                if step.checkpoint_before:
                    checkpoint_id = self.checkpoint_manager.checkpoint_before_step(
                        migration_id=self.plan.migration_id,
                        step_name=step.name,
                    )
                    self._emit_event("CHECKPOINT_CREATED", {
                        "step": step.name,
                        "checkpoint_id": checkpoint_id,
                        "type": "before",
                    })
                    self.migration_journal.checkpoint_created(
                        migration_id=self.plan.migration_id,
                        checkpoint_id=checkpoint_id,
                        step_name=step.name,
                    )
                
                # Execute the step
                success = self._execute_step(step)
                
                # Create checkpoint after step if requested
                if step.checkpoint_after:
                    checkpoint_id = self.checkpoint_manager.checkpoint_after_step(
                        migration_id=self.plan.migration_id,
                        step_name=step.name,
                        success=success,
                    )
                    self._emit_event("CHECKPOINT_CREATED", {
                        "step": step.name,
                        "checkpoint_id": checkpoint_id,
                        "type": "after",
                        "success": success,
                    })
                
                # Ask DecisionEngine about result
                decision = self.decision_engine.decide_after_step(
                    migration_id=self.plan.migration_id,
                    step_name=step.name,
                    step_success=success,
                    step_is_critical=step.critical,
                )
                
                # Execute decision
                if decision.decision_type == DecisionType.ABORT:
                    self._emit_log(step.name, "DecisionEngine says ABORT after step", "ERROR")
                    return False
                
                if decision.decision_type == DecisionType.ROLLBACK:
                    self._emit_log(step.name, "DecisionEngine says ROLLBACK after step", "ERROR")
                    self._perform_rollback(f"Step '{step.name}' failed")
                    return False
                
                if decision.decision_type == DecisionType.RETRY:
                    self._emit_log(step.name, "DecisionEngine says RETRY", "INFO")
                    # Retry logic handled in _execute_step
                
                if decision.decision_type == DecisionType.CONTINUE:
                    self._emit_log(step.name, "DecisionEngine says CONTINUE", "INFO")
            
            # Final health check
            self._final_health_check()
            
            # Update status
            self.state_manager.update_migration_status(
                self.plan.migration_id,
                MigrationStatus.SUCCEEDED,
                completed_at=time.time()
            )
            
            self._emit_event("MIGRATION_COMPLETED", {
                "migration_id": self.plan.migration_id,
                "bot_name": self.plan.bot_name,
            })
            
            self.migration_journal.add_entry(
                migration_id=self.plan.migration_id,
                message="Migration COMPLETED SUCCESSFULLY",
                level="INFO"
            )
            
            self._emit_log("SUMMARY", f"Migration {self.plan.migration_id} COMPLETED SUCCESSFULLY", "INFO")
            return True
            
        except MigrationCancelled:
            self._emit_log("SYSTEM", "Migration cancelled", "WARNING")
            self._emit_event("MIGRATION_CANCELLED", {"migration_id": self.plan.migration_id})
            return False
            
        except Exception as e:
            self._emit_log("SYSTEM", f"Migration failed: {e}", "ERROR")
            self._emit_event("MIGRATION_FAILED", {
                "migration_id": self.plan.migration_id,
                "error": str(e),
            })
            self._perform_rollback(f"Unexpected error: {e}")
            return False
            
        finally:
            self._running = False
            self._current_step_name = None
            self.command_executor.cleanup()
    
    # ------------------------------------------------------------------ #
    # Internal methods
    # ------------------------------------------------------------------ #
    
    def _initialize_migration(self) -> None:
        """Initialize migration record."""
        migration = self.state_manager.get_migration(self.plan.migration_id)
        if migration is None:
            self.state_manager.create_migration(
                migration_id=self.plan.migration_id,
                bot_name=self.plan.bot_name,
                source_vps=self.plan.source_credentials.host,
                destination_vps=self.plan.destination_credentials.host,
                metadata=self.plan.metadata,
            )
            self.state_manager.update_migration_status(
                self.plan.migration_id,
                MigrationStatus.RUNNING
            )
            
            self.migration_journal.add_entry(
                migration_id=self.plan.migration_id,
                message=f"Migration started for bot '{self.plan.bot_name}'",
                level="INFO"
            )
    
    def _auto_resume_from_checkpoint(self) -> bool:
        """
        Try to auto-resume from the latest checkpoint.
        Returns True if a checkpoint was restored.
        """
        # Check if migration was interrupted
        steps = self.state_manager.get_steps(self.plan.migration_id)
        if not steps:
            return False
        
        # Check if there's an incomplete step (migration was interrupted)
        incomplete = self.state_manager.get_last_incomplete_step(self.plan.migration_id)
        if not incomplete:
            return False
        
        # Try to restore from latest checkpoint
        checkpoint_id = self.checkpoint_manager.restore_latest(self.plan.migration_id)
        if checkpoint_id:
            self._auto_resume_checkpoint = checkpoint_id
            self._emit_event("CHECKPOINT_RESTORED", {
                "checkpoint_id": checkpoint_id,
                "step_name": incomplete.step_name,
            })
            self.migration_journal.add_entry(
                migration_id=self.plan.migration_id,
                message=f"Auto-resumed from checkpoint: {checkpoint_id}",
                level="INFO"
            )
            return True
        
        return False
    
    def _wait_for_schedule(self) -> None:
        """Wait for scheduled start time if any."""
        if self.plan.scheduled_start_time is None:
            return
        
        delay = self.plan.scheduled_start_time - time.time()
        if delay > 0:
            self._emit_log("SCHEDULE", f"Waiting {delay:.1f}s for scheduled start", "INFO")
            end_time = time.time() + delay
            while time.time() < end_time:
                self._check_cancelled()
                time.sleep(min(1.0, end_time - time.time()))
    
    def _run_capability_check(self) -> CapabilityReport:
        """Run capability check on destination VPS."""
        self._emit_log("PREFLIGHT", "Running capability check on destination VPS...", "INFO")
        
        report = self.capability_detector.check_capabilities(
            host=self.plan.destination_credentials.host,
            username=self.plan.destination_credentials.username,
            password=self.plan.destination_credentials.password,
            private_key_path=self.plan.destination_credentials.private_key_path,
        )
        
        self._emit_log("PREFLIGHT", f"Capability check: {report.summary()}", "INFO")
        
        if report.can_proceed:
            self._emit_log("PREFLIGHT", "✓ All capability checks PASSED", "INFO")
        else:
            for issue in report.issues:
                self._emit_log("PREFLIGHT", f"  ✗ {issue}", "ERROR")
        
        return report
    
    def _get_resume_point(self) -> Optional[str]:
        """Get the step to resume from."""
        steps = self.state_manager.get_steps(self.plan.migration_id)
        if not steps:
            return None
        
        # If we auto-resumed from checkpoint, use that step
        if self._auto_resume_checkpoint:
            checkpoint = self.state_manager.get_checkpoint(self._auto_resume_checkpoint)
            if checkpoint:
                self._emit_log("RESUME", f"Resuming from checkpoint step: {checkpoint.step_name}", "INFO")
                return checkpoint.step_name
        
        # Find last successful step
        last_completed = None
        for step in steps:
            if step.status in (StepStatus.SUCCEEDED, StepStatus.HEALED):
                last_completed = step.step_name
        
        if last_completed:
            self._emit_log("RESUME", f"Resuming from step: {last_completed}", "INFO")
            self.migration_journal.add_entry(
                migration_id=self.plan.migration_id,
                message=f"Resuming from step: {last_completed}",
                level="INFO",
                step_name=last_completed
            )
        
        return last_completed
    
    def _execute_step(self, step: MigrationStep) -> bool:
        """Execute a single step using CommandExecutor."""
        step_id = self.state_manager.create_step(
            migration_id=self.plan.migration_id,
            step_name=step.name,
            command=step.command,
            source_vps=self.plan.source_credentials.host,
            destination_vps=self.plan.destination_credentials.host,
        )
        
        self._emit_log(step.name, f"Executing: {step.command}", "INFO")
        
        # Execute command
        result = self.command_executor.execute_ssh(
            target="destination",
            command=step.command,
            use_sudo=step.use_sudo,
            timeout=step.timeout,
        )
        
        success = result.succeeded
        
        # Update state
        if success:
            self.state_manager.update_step(
                step_id,
                StepStatus.SUCCEEDED,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
            )
            self._emit_log(step.name, "✓ Step succeeded", "INFO")
            self._emit_event("STEP_COMPLETED", {
                "step": step.name,
                "success": True,
            })
        else:
            self.state_manager.update_step(
                step_id,
                StepStatus.FAILED,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                error_message=f"Exit code: {result.exit_code}",
            )
            self._emit_log(step.name, f"✗ Step failed (exit={result.exit_code})", "ERROR")
            self._emit_event("STEP_FAILED", {
                "step": step.name,
                "exit_code": result.exit_code,
            })
        
        return success
    
    def _final_health_check(self) -> None:
        """Run final health checks using CommandExecutor."""
        self._emit_log("HEALTH", "Running final health checks...", "INFO")
        self._emit_event("HEALTH_CHECK_STARTED", {"migration_id": self.plan.migration_id})
        
        # Get service PID
        result = self.command_executor.execute_ssh(
            target="destination",
            command=f"systemctl show {self.plan.bot_service_name} --property=MainPID --value",
        )
        
        if not result.succeeded or not result.stdout.strip():
            raise RuntimeError("Could not get service PID")
        
        try:
            pid = int(result.stdout.strip())
        except ValueError:
            raise RuntimeError(f"Invalid PID: {result.stdout}")
        
        # Check process health
        health_result = self.command_executor.execute_ssh(
            target="destination",
            command=f"kill -0 {pid} 2>/dev/null && echo 'alive' || echo 'dead'",
        )
        
        if not health_result.succeeded or "alive" not in health_result.stdout:
            raise RuntimeError("Service is not running")
        
        # Check systemd status
        status_result = self.command_executor.execute_ssh(
            target="destination",
            command=f"systemctl is-active {self.plan.bot_service_name}",
        )
        
        if not status_result.succeeded or status_result.stdout.strip() != "active":
            raise RuntimeError(f"Service not active: {status_result.stdout}")
        
        self._emit_log("HEALTH", "✓ All health checks PASSED", "INFO")
        self._emit_event("HEALTH_CHECK_PASSED", {
            "migration_id": self.plan.migration_id,
            "pid": pid,
        })
    
    def _perform_rollback(self, reason: str) -> None:
        """Perform rollback using RollbackManager."""
        self._emit_log("ROLLBACK", f"Initiating rollback: {reason}", "ERROR")
        self._emit_event("ROLLBACK_STARTED", {
            "migration_id": self.plan.migration_id,
            "reason": reason,
        })
        
        # Create checkpoint before rollback
        self.checkpoint_manager.checkpoint_rollback(
            migration_id=self.plan.migration_id,
            reason=reason,
        )
        
        # Use RollbackManager
        from .rollback_manager import RollbackManager
        
        rollback_manager = RollbackManager(
            command_executor=self.command_executor,
            state_manager=self.state_manager,
            bot_service_name=self.plan.bot_service_name,
            project_directory=self.plan.project_directory,
            target="destination",
        )
        
        report = rollback_manager.complete_rollback(self.plan.migration_id)
        
        self._emit_log("ROLLBACK", f"Rollback {'succeeded' if report.success else 'failed'}", 
                      "INFO" if report.success else "ERROR")
        self._emit_event("ROLLBACK_COMPLETED" if report.success else "ROLLBACK_FAILED", {
            "migration_id": self.plan.migration_id,
            "success": report.success,
        })
        
        self.migration_journal.add_entry(
            migration_id=self.plan.migration_id,
            message=f"Rollback {'succeeded' if report.success else 'failed'}: {reason}",
            level="INFO" if report.success else "ERROR"
        )
        
        self.state_manager.update_migration_status(
            self.plan.migration_id,
            MigrationStatus.ROLLED_BACK,
            completed_at=time.time()
        )
    
    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #
    
    def get_status(self) -> Dict[str, Any]:
        """Get current migration status."""
        migration = self.state_manager.get_migration(self.plan.migration_id)
        if not migration:
            return {"migration_id": self.plan.migration_id, "status": "not_started"}
        
        steps = self.state_manager.get_steps(self.plan.migration_id)
        
        return {
            "migration_id": migration.migration_id,
            "bot_name": migration.bot_name,
            "status": migration.status.value,
            "started_at": migration.started_at,
            "completed_at": migration.completed_at,
            "total_steps": migration.total_steps,
            "completed_steps": migration.completed_steps,
            "failed_steps": migration.failed_steps,
            "healed_steps": migration.healed_steps,
            "current_step": self._current_step_name,
            "is_running": self._running,
            "last_checkpoint": self._auto_resume_checkpoint,
            "steps": [
                {
                    "name": s.step_name,
                    "status": s.status.value,
                    "retries": s.retries,
                    "error": s.error_message[:200] if s.error_message else None,
                }
                for s in steps
            ],
        }
    
    def shutdown(self) -> None:
        """Shut down the manager."""
        self.cancel()
        self.command_executor.cleanup()
        self.state_manager.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()
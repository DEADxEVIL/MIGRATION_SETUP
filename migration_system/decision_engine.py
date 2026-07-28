from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from .state_manager import StateManager, StepStatus, MigrationStatus
from .error_pattern_library import find_matches, best_match

logger = logging.getLogger("decision_engine")


class DecisionType(str, Enum):
    """Possible decisions."""
    CONTINUE = "continue"
    RETRY = "retry"
    HEAL = "heal"
    ROLLBACK = "rollback"
    ABORT = "abort"
    WAIT = "wait"


@dataclass
class Decision:
    """Structured decision result."""
    decision_type: DecisionType
    reason: str
    confidence: float = 1.0
    data: Optional[Dict[str, Any]] = None
    should_retry: bool = False
    should_heal: bool = False
    heal_commands: List[str] = None
    
    def __post_init__(self):
        if self.heal_commands is None:
            self.heal_commands = []
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_type": self.decision_type.value,
            "reason": self.reason,
            "confidence": self.confidence,
            "data": self.data,
            "should_retry": self.should_retry,
            "should_heal": self.should_heal,
            "heal_commands": self.heal_commands,
        }


class DecisionEngine:
    """
    PURE DECISION MAKER.
    
    Never executes anything. Only analyzes and decides.
    """
    
    def __init__(
        self,
        state_manager: StateManager,
        min_confidence_to_auto_heal: float = 0.6,
        allow_risky_fixes: bool = False,
        max_retries: int = 3,
    ):
        self.state_manager = state_manager
        self.min_confidence_to_auto_heal = min_confidence_to_auto_heal
        self.allow_risky_fixes = allow_risky_fixes
        self.max_retries = max_retries
    
    # ------------------------------------------------------------------ #
    # Main decision methods
    # ------------------------------------------------------------------ #
    
    def decide_before_step(
        self,
        migration_id: str,
        step_name: str,
        step_index: int,
        total_steps: int,
    ) -> Decision:
        """
        Decide what to do before executing a step.
        
        Checks:
        - Is migration cancelled?
        - Is there a checkpoint to restore?
        - Should we skip this step?
        """
        # Check if migration is already failed
        migration = self.state_manager.get_migration(migration_id)
        if migration and migration.status in (MigrationStatus.FAILED, MigrationStatus.ROLLED_BACK):
            return Decision(
                decision_type=DecisionType.ABORT,
                reason=f"Migration status is {migration.status.value}"
            )
        
        # Check for cancellation
        if self._is_cancelled(migration_id):
            return Decision(
                decision_type=DecisionType.ABORT,
                reason="Migration was cancelled"
            )
        
        # Check if step already completed
        steps = self.state_manager.get_steps(migration_id)
        for step in steps:
            if step.step_name == step_name and step.status == StepStatus.SUCCEEDED:
                return Decision(
                    decision_type=DecisionType.CONTINUE,
                    reason="Step already completed"
                )
        
        # Check if step was failed and needs retry
        for step in steps:
            if step.step_name == step_name and step.status == StepStatus.FAILED:
                if step.retries < self.max_retries:
                    return Decision(
                        decision_type=DecisionType.RETRY,
                        reason=f"Step failed previously, retry {step.retries + 1}/{self.max_retries}",
                        should_retry=True,
                    )
                else:
                    return Decision(
                        decision_type=DecisionType.ROLLBACK,
                        reason=f"Step failed {step.retries} times, max retries exceeded"
                    )
        
        # Default: continue
        return Decision(
            decision_type=DecisionType.CONTINUE,
            reason="Proceed with step",
        )
    
    def decide_after_step(
        self,
        migration_id: str,
        step_name: str,
        step_success: bool,
        step_is_critical: bool = True,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
    ) -> Decision:
        """
        Decide what to do after executing a step.
        
        If success: CONTINUE
        If failure: Try to heal, retry, or rollback
        """
        if step_success:
            return Decision(
                decision_type=DecisionType.CONTINUE,
                reason="Step succeeded"
            )
        
        # Step failed - analyze for healing
        return self._decide_on_failure(
            migration_id=migration_id,
            step_name=step_name,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            step_is_critical=step_is_critical,
        )
    
    def decide_on_error(
        self,
        migration_id: str,
        step_name: str,
        error_message: str,
        error_stdout: str = "",
        error_stderr: str = "",
        exit_code: int = 0,
    ) -> Decision:
        """Decide what to do when an error occurs."""
        return self._decide_on_failure(
            migration_id=migration_id,
            step_name=step_name,
            stdout=error_stdout,
            stderr=error_stderr,
            exit_code=exit_code,
            step_is_critical=True,
        )
    
    # ------------------------------------------------------------------ #
    # Internal decision methods
    # ------------------------------------------------------------------ #
    
    def _decide_on_failure(
        self,
        migration_id: str,
        step_name: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        step_is_critical: bool,
    ) -> Decision:
        """Decide what to do when a step fails."""
        
        # Get step history
        steps = self.state_manager.get_steps(migration_id)
        step_history = None
        for s in steps:
            if s.step_name == step_name:
                step_history = s
                break
        
        retries = step_history.retries if step_history else 0
        
        # Check if we can heal
        heal_result = self._can_heal(stdout, stderr, exit_code, retries)
        
        if heal_result["can_heal"]:
            return Decision(
                decision_type=DecisionType.HEAL,
                reason=f"Found known error pattern: {heal_result['pattern_description']}",
                confidence=heal_result["confidence"],
                should_heal=True,
                heal_commands=heal_result["fix_commands"],
                data={
                    "pattern": heal_result["pattern_category"],
                    "confidence": heal_result["confidence"],
                }
            )
        
        # Check if we should retry
        if retries < self.max_retries:
            return Decision(
                decision_type=DecisionType.RETRY,
                reason=f"Step failed, retry {retries + 1}/{self.max_retries}",
                should_retry=True,
                confidence=0.5,
            )
        
        # Check if critical
        if step_is_critical:
            return Decision(
                decision_type=DecisionType.ROLLBACK,
                reason=f"Critical step '{step_name}' failed after {retries} retries",
                confidence=1.0,
            )
        
        # Non-critical failure - abort
        return Decision(
            decision_type=DecisionType.ABORT,
            reason=f"Non-critical step '{step_name}' failed after {retries} retries",
            confidence=0.5,
        )
    
    def _can_heal(self, stdout: str, stderr: str, exit_code: int, retries: int) -> Dict[str, Any]:
        """
        Check if the error can be healed.
        
        Returns:
            {
                "can_heal": bool,
                "pattern_category": str,
                "pattern_description": str,
                "fix_commands": List[str],
                "confidence": float,
            }
        """
        combined_text = f"{stdout}\n{stderr}"
        
        # Find matching patterns
        matches = find_matches(combined_text)
        
        if not matches:
            return {
                "can_heal": False,
                "pattern_category": "unknown",
                "pattern_description": "No known pattern matched",
                "fix_commands": [],
                "confidence": 0.0,
            }
        
        best = matches[0]
        
        # Check if safe to auto-execute
        if not best.safe_to_auto_execute and not self.allow_risky_fixes:
            return {
                "can_heal": False,
                "pattern_category": best.category,
                "pattern_description": best.description,
                "fix_commands": list(best.fix_commands),
                "confidence": best.confidence,
            }
        
        # Check confidence threshold
        if best.confidence < self.min_confidence_to_auto_heal:
            return {
                "can_heal": False,
                "pattern_category": best.category,
                "pattern_description": f"Low confidence: {best.confidence}",
                "fix_commands": list(best.fix_commands),
                "confidence": best.confidence,
            }
        
        # Check if already tried this fix
        # (Healing history is stored in state manager)
        
        return {
            "can_heal": True,
            "pattern_category": best.category,
            "pattern_description": best.description,
            "fix_commands": list(best.fix_commands),
            "confidence": best.confidence,
        }
    
    def _is_cancelled(self, migration_id: str) -> bool:
        """Check if migration was cancelled."""
        migration = self.state_manager.get_migration(migration_id)
        if not migration:
            return False
        
        # Check status in state manager (cancelled = FAILED)
        return migration.status == MigrationStatus.FAILED
    
    # ------------------------------------------------------------------ #
    # Utility methods
    # ------------------------------------------------------------------ #
    
    def should_retry(self, error_hash: str, step_name: str) -> bool:
        """Check if we should retry based on error history."""
        memory = self.state_manager.get_error_memory(error_hash)
        if not memory:
            return True  # Unknown error, retry
        
        # If success rate is very low, don't retry
        if memory.success_rate < 0.1:
            return False
        
        return True
    
    def get_best_fix(self, error_hash: str) -> Optional[Dict[str, Any]]:
        """Get best fix for an error."""
        memory = self.state_manager.get_error_memory(error_hash)
        if not memory:
            return None
        
        return {
            "fix_commands": memory.fix_commands,
            "success_rate": memory.success_rate,
            "times_used": memory.success_count + memory.failure_count,
        }
    
    def record_decision(self, migration_id: str, decision: Decision) -> None:
        """Record a decision for audit."""
        self.state_manager.journal_entry(
            migration_id=migration_id,
            message=f"Decision: {decision.decision_type.value} - {decision.reason}",
            level="INFO"
        )
    
    def get_decision_history(self, migration_id: str) -> List[Dict[str, Any]]:
        """Get decision history from journal."""
        entries = self.state_manager.get_journal(migration_id, limit=100)
        return [
            e for e in entries
            if "Decision:" in e.get("message", "")
        ]
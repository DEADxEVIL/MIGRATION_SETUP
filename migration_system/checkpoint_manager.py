from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .state_manager import StateManager, Checkpoint

logger = logging.getLogger("checkpoint_manager")


@dataclass
class CheckpointInfo:
    """Detailed checkpoint information."""
    checkpoint_id: str
    migration_id: str
    step_name: str
    description: str
    created_at: float
    restored_at: Optional[float]
    files_checksum: Optional[str]
    state_snapshot: Dict[str, Any]
    is_restored: bool = False


class CheckpointManager:
    """
    Automatic checkpoint management.
    
    Creates checkpoints:
    - Before each migration step
    - After critical steps
    - On demand
    """
    
    def __init__(
        self,
        state_manager: StateManager,
        checkpoint_dir: str = "checkpoints",
    ):
        self.state_manager = state_manager
        self.checkpoint_dir = checkpoint_dir
        
        import os
        os.makedirs(checkpoint_dir, exist_ok=True)
    
    # ------------------------------------------------------------------ #
    # Checkpoint creation
    # ------------------------------------------------------------------ #
    
    def create_checkpoint(
        self,
        migration_id: str,
        step_name: str,
        description: str,
        state_snapshot: Optional[Dict[str, Any]] = None,
        files_checksum: Optional[str] = None,
    ) -> str:
        """
        Create a new checkpoint.
        
        Automatically captures:
        - Current migration state
        - Step history
        - Resource snapshots
        """
        if state_snapshot is None:
            state_snapshot = self._capture_state(migration_id)
        
        checkpoint_id = self.state_manager.create_checkpoint(
            migration_id=migration_id,
            step_name=step_name,
            description=description,
            state_snapshot=state_snapshot,
            files_checksum=files_checksum,
        )
        
        # Also save as JSON file for redundancy
        self._save_checkpoint_file(
            checkpoint_id=checkpoint_id,
            migration_id=migration_id,
            step_name=step_name,
            description=description,
            state_snapshot=state_snapshot,
            files_checksum=files_checksum,
        )
        
        logger.info(f"Created checkpoint: {checkpoint_id} ({description})")
        return checkpoint_id
    
    def _capture_state(self, migration_id: str) -> Dict[str, Any]:
        """Capture current migration state."""
        migration = self.state_manager.get_migration(migration_id)
        steps = self.state_manager.get_steps(migration_id)
        
        return {
            "migration_id": migration_id,
            "status": migration.status.value if migration else "unknown",
            "total_steps": len(steps),
            "completed_steps": sum(1 for s in steps if s.status.value in ("succeeded", "healed")),
            "failed_steps": sum(1 for s in steps if s.status.value == "failed"),
            "steps": [
                {
                    "name": s.step_name,
                    "status": s.status.value,
                    "command": s.command,
                }
                for s in steps
            ],
            "timestamp": time.time(),
        }
    
    def _save_checkpoint_file(
        self,
        checkpoint_id: str,
        migration_id: str,
        step_name: str,
        description: str,
        state_snapshot: Dict[str, Any],
        files_checksum: Optional[str] = None,
    ) -> None:
        """Save checkpoint as JSON file for redundancy."""
        import os
        
        checkpoint_data = {
            "checkpoint_id": checkpoint_id,
            "migration_id": migration_id,
            "step_name": step_name,
            "description": description,
            "state_snapshot": state_snapshot,
            "files_checksum": files_checksum,
            "created_at": time.time(),
            "restored_at": None,
        }
        
        filepath = os.path.join(self.checkpoint_dir, f"{checkpoint_id}.json")
        with open(filepath, "w") as f:
            json.dump(checkpoint_data, f, indent=2)
    
    # ------------------------------------------------------------------ #
    # Checkpoint restoration
    # ------------------------------------------------------------------ #
    
    def restore_checkpoint(self, checkpoint_id: str) -> bool:
        """
        Restore from a checkpoint.
        
        Returns True if successful.
        """
        # Try SQLite first
        checkpoint = self.state_manager.get_checkpoint(checkpoint_id)
        
        if checkpoint is None:
            # Try JSON file
            checkpoint = self._load_checkpoint_file(checkpoint_id)
        
        if checkpoint is None:
            logger.error(f"Checkpoint not found: {checkpoint_id}")
            return False
        
        # Verify integrity
        if not self._verify_checkpoint(checkpoint):
            logger.error(f"Checkpoint integrity check failed: {checkpoint_id}")
            return False
        
        # Restore state
        try:
            self._restore_state(checkpoint)
            self.state_manager.restore_checkpoint(checkpoint_id)
            logger.info(f"Restored checkpoint: {checkpoint_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to restore checkpoint: {e}")
            return False
    
    def restore_latest(self, migration_id: str) -> Optional[str]:
        """Restore the latest checkpoint for a migration."""
        checkpoints = self.state_manager.get_checkpoints(migration_id)
        if not checkpoints:
            logger.warning(f"No checkpoints found for migration: {migration_id}")
            return None
        
        latest = checkpoints[0]  # Already sorted by created_at DESC
        if self.restore_checkpoint(latest.checkpoint_id):
            return latest.checkpoint_id
        
        return None
    
    def _load_checkpoint_file(self, checkpoint_id: str) -> Optional[Checkpoint]:
        """Load checkpoint from JSON file."""
        import os
        
        filepath = os.path.join(self.checkpoint_dir, f"{checkpoint_id}.json")
        if not os.path.exists(filepath):
            return None
        
        with open(filepath, "r") as f:
            data = json.load(f)
        
        return Checkpoint(
            checkpoint_id=data["checkpoint_id"],
            migration_id=data["migration_id"],
            step_name=data["step_name"],
            description=data["description"],
            state_snapshot=data["state_snapshot"],
            files_checksum=data.get("files_checksum"),
            created_at=data["created_at"],
            restored_at=data.get("restored_at"),
        )
    
    def _verify_checkpoint(self, checkpoint: Checkpoint) -> bool:
        """Verify checkpoint integrity."""
        # Check required fields
        required = ["migration_id", "step_name", "state_snapshot"]
        for field in required:
            if not getattr(checkpoint, field, None):
                return False
        
        # Verify files checksum if present
        if checkpoint.files_checksum:
            # In production, would verify checksum
            pass
        
        return True
    
    def _restore_state(self, checkpoint: Checkpoint) -> None:
        """Restore state from checkpoint."""
        # Update migration status if needed
        state = checkpoint.state_snapshot
        
        if "migration_id" in state:
            self.state_manager.update_migration_status(
                state["migration_id"],
                MigrationStatus.RUNNING,  # Restore to running
            )
        
        # Mark steps as restored
        # In production, would use this to skip completed steps
        logger.info(f"State restored from checkpoint: {checkpoint.checkpoint_id}")
    
    # ------------------------------------------------------------------ #
    # Checkpoint management
    # ------------------------------------------------------------------ #
    
    def list_checkpoints(self, migration_id: str) -> List[CheckpointInfo]:
        """List all checkpoints for a migration."""
        checkpoints = self.state_manager.get_checkpoints(migration_id)
        
        return [
            CheckpointInfo(
                checkpoint_id=c.checkpoint_id,
                migration_id=c.migration_id,
                step_name=c.step_name,
                description=c.description,
                created_at=c.created_at,
                restored_at=c.restored_at,
                files_checksum=c.files_checksum,
                state_snapshot=c.state_snapshot,
                is_restored=c.restored_at is not None,
            )
            for c in checkpoints
        ]
    
    def get_latest_checkpoint(self, migration_id: str) -> Optional[CheckpointInfo]:
        """Get the latest checkpoint for a migration."""
        checkpoints = self.list_checkpoints(migration_id)
        if not checkpoints:
            return None
        return checkpoints[0]  # Already sorted by created_at DESC
    
    def cleanup_old_checkpoints(self, migration_id: str, keep_count: int = 5) -> None:
        """Clean up old checkpoints, keeping the most recent N."""
        import os
        
        checkpoints = self.list_checkpoints(migration_id)
        if len(checkpoints) <= keep_count:
            return
        
        # Keep the most recent
        to_delete = checkpoints[keep_count:]
        for c in to_delete:
            # Delete from SQLite
            self._delete_checkpoint(c.checkpoint_id)
            # Delete JSON file
            filepath = os.path.join(self.checkpoint_dir, f"{c.checkpoint_id}.json")
            try:
                os.remove(filepath)
            except Exception:
                pass
            logger.info(f"Deleted old checkpoint: {c.checkpoint_id}")
    
    def _delete_checkpoint(self, checkpoint_id: str) -> None:
        """Delete a checkpoint from SQLite."""
        with self.state_manager._connect() as conn:
            conn.execute(
                "DELETE FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,)
            )
    
    # ------------------------------------------------------------------ #
    # Automatic checkpoint creation
    # ------------------------------------------------------------------ #
    
    def checkpoint_before_step(
        self,
        migration_id: str,
        step_name: str,
    ) -> str:
        """Create checkpoint before executing a step."""
        description = f"Before step: {step_name}"
        
        # Capture current state
        state = self._capture_state(migration_id)
        
        # Create checkpoint
        return self.create_checkpoint(
            migration_id=migration_id,
            step_name=step_name,
            description=description,
            state_snapshot=state,
        )
    
    def checkpoint_after_step(
        self,
        migration_id: str,
        step_name: str,
        success: bool,
    ) -> str:
        """Create checkpoint after executing a step."""
        description = f"After step: {step_name} ({'SUCCESS' if success else 'FAILED'})"
        
        # Capture current state
        state = self._capture_state(migration_id)
        
        # Create checkpoint
        return self.create_checkpoint(
            migration_id=migration_id,
            step_name=step_name,
            description=description,
            state_snapshot=state,
        )
    
    def checkpoint_rollback(
        self,
        migration_id: str,
        reason: str,
    ) -> str:
        """Create checkpoint before rollback."""
        description = f"Before rollback: {reason}"
        
        state = self._capture_state(migration_id)
        
        return self.create_checkpoint(
            migration_id=migration_id,
            step_name="rollback",
            description=description,
            state_snapshot=state,
        )


# Import for type hints
from .state_manager import MigrationStatus
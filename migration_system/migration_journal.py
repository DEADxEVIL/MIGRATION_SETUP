from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .state_manager import StateManager

logger = logging.getLogger("migration_journal")


@dataclass
class JournalEntry:
    """A single journal entry."""
    timestamp: float
    message: str
    level: str = "INFO"
    step_name: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "time_str": time.ctime(self.timestamp),
            "level": self.level,
            "step_name": self.step_name,
            "message": self.message,
            "data": self.data,
        }
    
    def to_human(self) -> str:
        """Get human-readable string."""
        time_str = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        step = f"[{self.step_name}] " if self.step_name else ""
        return f"{time_str} {step}{self.message}"


class MigrationJournal:
    """
    Human-readable migration journal.
    
    Maintains a complete, human-readable record of every migration.
    """
    
    def __init__(self, state_manager: StateManager):
        self.state_manager = state_manager
    
    # ------------------------------------------------------------------ #
    # Adding entries
    # ------------------------------------------------------------------ #
    
    def add_entry(
        self,
        migration_id: str,
        message: str,
        level: str = "INFO",
        step_name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Add an entry to the journal.
        
        Returns entry ID.
        """
        entry_id = self.state_manager.add_journal_entry(
            migration_id=migration_id,
            message=message,
            level=level,
            step_name=step_name,
            data=data,
        )
        
        # Log to console as well
        entry = JournalEntry(
            timestamp=time.time(),
            message=message,
            level=level,
            step_name=step_name,
            data=data,
        )
        logger.info(f"[JOURNAL] {entry.to_human()}")
        
        return entry_id
    
    # ------------------------------------------------------------------ #
    # Convenience methods
    # ------------------------------------------------------------------ #
    
    def info(
        self,
        migration_id: str,
        message: str,
        step_name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Add INFO entry."""
        return self.add_entry(migration_id, message, "INFO", step_name, data)
    
    def warning(
        self,
        migration_id: str,
        message: str,
        step_name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Add WARNING entry."""
        return self.add_entry(migration_id, message, "WARNING", step_name, data)
    
    def error(
        self,
        migration_id: str,
        message: str,
        step_name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Add ERROR entry."""
        return self.add_entry(migration_id, message, "ERROR", step_name, data)
    
    def success(
        self,
        migration_id: str,
        message: str,
        step_name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Add SUCCESS entry."""
        return self.add_entry(migration_id, message, "SUCCESS", step_name, data)
    
    def step_started(
        self,
        migration_id: str,
        step_name: str,
        message: Optional[str] = None,
    ) -> int:
        """Log step started."""
        msg = message or f"Starting step: {step_name}"
        return self.add_entry(migration_id, msg, "INFO", step_name)
    
    def step_completed(
        self,
        migration_id: str,
        step_name: str,
        success: bool,
        message: Optional[str] = None,
    ) -> int:
        """Log step completed."""
        status = "SUCCESS" if success else "FAILED"
        msg = message or f"Step {step_name} completed with {status}"
        return self.add_entry(migration_id, msg, status, step_name)
    
    def healing_applied(
        self,
        migration_id: str,
        step_name: str,
        fix_command: str,
        success: bool,
    ) -> int:
        """Log healing attempt."""
        status = "SUCCESS" if success else "FAILED"
        msg = f"Healing applied: {fix_command} - {status}"
        return self.add_entry(migration_id, msg, status, step_name)
    
    def rollback_started(
        self,
        migration_id: str,
        reason: str,
    ) -> int:
        """Log rollback started."""
        msg = f"Rollback started: {reason}"
        return self.add_entry(migration_id, msg, "WARNING")
    
    def rollback_completed(
        self,
        migration_id: str,
        success: bool,
    ) -> int:
        """Log rollback completed."""
        status = "SUCCESS" if success else "FAILED"
        msg = f"Rollback completed with {status}"
        return self.add_entry(migration_id, msg, status)
    
    def checkpoint_created(
        self,
        migration_id: str,
        checkpoint_id: str,
        step_name: str,
    ) -> int:
        """Log checkpoint creation."""
        msg = f"Checkpoint created: {checkpoint_id} (step: {step_name})"
        return self.add_entry(migration_id, msg, "INFO", step_name)
    
    # ------------------------------------------------------------------ #
    # Reading entries
    # ------------------------------------------------------------------ #
    
    def get_journal(
        self,
        migration_id: str,
        limit: int = 1000,
        level: Optional[str] = None,
    ) -> List[JournalEntry]:
        """Get journal entries for a migration."""
        entries = self.state_manager.get_journal(migration_id, limit)
        
        journal_entries = []
        for e in entries:
            entry = JournalEntry(
                timestamp=e["timestamp"],
                message=e["message"],
                level=e["level"],
                step_name=e.get("step_name"),
                data=e.get("data"),
            )
            journal_entries.append(entry)
        
        # Filter by level
        if level:
            journal_entries = [e for e in journal_entries if e.level == level]
        
        return journal_entries
    
    def get_since(
        self,
        migration_id: str,
        since_timestamp: float,
    ) -> List[JournalEntry]:
        """Get journal entries since a timestamp."""
        entries = self.state_manager.get_journal_since(migration_id, since_timestamp)
        
        return [
            JournalEntry(
                timestamp=e["timestamp"],
                message=e["message"],
                level=e["level"],
                step_name=e.get("step_name"),
                data=e.get("data"),
            )
            for e in entries
        ]
    
    def get_by_step(
        self,
        migration_id: str,
        step_name: str,
    ) -> List[JournalEntry]:
        """Get journal entries for a specific step."""
        entries = self.get_journal(migration_id)
        return [e for e in entries if e.step_name == step_name]
    
    def get_timeline(self, migration_id: str) -> str:
        """Get human-readable timeline."""
        entries = self.get_journal(migration_id, limit=500)
        
        if not entries:
            return "No journal entries found."
        
        lines = []
        for entry in entries:
            lines.append(entry.to_human())
        
        return "\n".join(lines)
    
    def get_summary(self, migration_id: str) -> Dict[str, Any]:
        """Get summary of journal entries."""
        entries = self.get_journal(migration_id)
        
        if not entries:
            return {"total": 0}
        
        levels = {}
        steps = set()
        
        for e in entries:
            levels[e.level] = levels.get(e.level, 0) + 1
            if e.step_name:
                steps.add(e.step_name)
        
        return {
            "total": len(entries),
            "levels": levels,
            "steps": len(steps),
            "first_entry": min(e.timestamp for e in entries),
            "last_entry": max(e.timestamp for e in entries),
        }


if __name__ == "__main__":
    from .state_manager import StateManager
    
    # Test journal
    sm = StateManager(":memory:")
    journal = MigrationJournal(sm)
    
    # Add entries
    journal.info("test-001", "Migration started")
    journal.step_started("test-001", "upload_files")
    journal.step_completed("test-001", "upload_files", True)
    journal.healing_applied("test-001", "install_deps", "apt-get install ca-certificates", True)
    journal.rollback_started("test-001", "SSL error")
    journal.rollback_completed("test-001", True)
    
    # Get timeline
    print(journal.get_timeline("test-001"))
    
    # Get summary
    print(journal.get_summary("test-001"))
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger("state_manager")


class StepStatus(str, Enum):
    """Status of a migration step."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"
    HEALED = "healed"
    CHECKPOINT = "checkpoint"


class MigrationStatus(str, Enum):
    """Overall migration status."""
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    HEALING = "healing"


@dataclass
class StepState:
    """Single migration step record."""
    step_id: Optional[int] = None
    migration_id: str = ""
    step_name: str = ""
    command: str = ""
    status: StepStatus = StepStatus.PENDING
    source_vps: str = ""
    destination_vps: str = ""
    retries: int = 0
    last_stdout: str = ""
    last_stderr: str = ""
    exit_code: Optional[int] = None
    error_message: str = ""
    error_hash: Optional[str] = None
    checkpoint_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class MigrationRecord:
    """Complete migration record."""
    migration_id: str
    bot_name: str
    source_vps: str
    destination_vps: str
    status: MigrationStatus
    started_at: float
    completed_at: Optional[float] = None
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0
    healed_steps: int = 0
    rollback_count: int = 0
    metadata: Optional[str] = None


@dataclass
class Checkpoint:
    """Migration checkpoint."""
    checkpoint_id: str
    migration_id: str
    step_name: str
    description: str
    state_snapshot: Dict[str, Any]
    files_checksum: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    restored_at: Optional[float] = None


@dataclass
class JournalEntry:
    """Human-readable journal entry."""
    entry_id: Optional[int] = None
    migration_id: str = ""
    timestamp: float = field(default_factory=time.time)
    level: str = "INFO"
    step_name: Optional[str] = None
    message: str = ""
    data: Optional[Dict[str, Any]] = None


@dataclass
class ResourceSnapshot:
    """Resource usage snapshot."""
    snapshot_id: Optional[int] = None
    migration_id: str = ""
    timestamp: float = field(default_factory=time.time)
    cpu_percent: float = 0.0
    ram_percent: float = 0.0
    disk_percent: float = 0.0
    process_count: int = 0
    details: Optional[Dict[str, Any]] = None


@dataclass
class HealingHistory:
    """Healing attempt history."""
    healing_id: Optional[int] = None
    migration_id: str = ""
    step_name: str = ""
    error_hash: str = ""
    diagnosis_category: str = ""
    fix_applied: str = ""
    success: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class ErrorMemory:
    """Deterministic error memory (AI memory)."""
    error_hash: str
    error_signature: str
    fix_commands: List[str]
    success_count: int = 0
    failure_count: int = 0
    os_version: str = ""
    python_version: str = ""
    package_versions: str = ""
    command_failed: str = ""
    step_context: str = ""
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    last_successful_fix: float = 0.0
    
    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0


class StateManagerError(Exception):
    """Raised for state management errors."""


class StateManager:
    """SQLite-backed state manager - SINGLE SOURCE OF TRUTH."""
    
    SCHEMA_VERSION = 3
    
    SCHEMA = """
    -- Schema version
    CREATE TABLE IF NOT EXISTS schema_info (
        version INTEGER NOT NULL
    );
    
    -- Migration records
    CREATE TABLE IF NOT EXISTS migrations (
        migration_id      TEXT PRIMARY KEY,
        bot_name          TEXT NOT NULL,
        source_vps        TEXT NOT NULL,
        destination_vps   TEXT NOT NULL,
        status            TEXT NOT NULL,
        started_at        REAL NOT NULL,
        completed_at      REAL,
        total_steps       INTEGER DEFAULT 0,
        completed_steps   INTEGER DEFAULT 0,
        failed_steps      INTEGER DEFAULT 0,
        healed_steps      INTEGER DEFAULT 0,
        rollback_count    INTEGER DEFAULT 0,
        metadata          TEXT
    );
    
    -- Migration steps
    CREATE TABLE IF NOT EXISTS migration_steps (
        step_id           INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_id      TEXT NOT NULL,
        step_name         TEXT NOT NULL,
        command           TEXT NOT NULL,
        status            TEXT NOT NULL,
        source_vps        TEXT NOT NULL,
        destination_vps   TEXT NOT NULL,
        retries           INTEGER NOT NULL DEFAULT 0,
        last_stdout       TEXT NOT NULL DEFAULT '',
        last_stderr       TEXT NOT NULL DEFAULT '',
        exit_code         INTEGER,
        error_message     TEXT NOT NULL DEFAULT '',
        error_hash        TEXT,
        checkpoint_id     TEXT,
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL,
        FOREIGN KEY (migration_id) REFERENCES migrations(migration_id)
    );
    
    -- Checkpoints
    CREATE TABLE IF NOT EXISTS checkpoints (
        checkpoint_id     TEXT PRIMARY KEY,
        migration_id      TEXT NOT NULL,
        step_name         TEXT NOT NULL,
        description       TEXT NOT NULL,
        state_snapshot    TEXT NOT NULL,
        files_checksum    TEXT,
        created_at        REAL NOT NULL,
        restored_at       REAL,
        FOREIGN KEY (migration_id) REFERENCES migrations(migration_id)
    );
    
    -- Migration journal (human-readable)
    CREATE TABLE IF NOT EXISTS migration_journal (
        entry_id          INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_id      TEXT NOT NULL,
        timestamp         REAL NOT NULL,
        level             TEXT NOT NULL,
        step_name         TEXT,
        message           TEXT NOT NULL,
        data              TEXT,
        FOREIGN KEY (migration_id) REFERENCES migrations(migration_id)
    );
    
    -- Events
    CREATE TABLE IF NOT EXISTS events (
        event_id          TEXT PRIMARY KEY,
        migration_id      TEXT NOT NULL,
        event_type        TEXT NOT NULL,
        data              TEXT,
        source            TEXT NOT NULL,
        category          TEXT NOT NULL,
        timestamp         REAL NOT NULL,
        correlation_id    TEXT,
        FOREIGN KEY (migration_id) REFERENCES migrations(migration_id)
    );
    
    -- Resource snapshots
    CREATE TABLE IF NOT EXISTS resource_snapshots (
        snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_id      TEXT NOT NULL,
        timestamp         REAL NOT NULL,
        cpu_percent       REAL NOT NULL,
        ram_percent       REAL NOT NULL,
        disk_percent      REAL NOT NULL,
        process_count     INTEGER NOT NULL,
        details           TEXT,
        FOREIGN KEY (migration_id) REFERENCES migrations(migration_id)
    );
    
    -- Healing history
    CREATE TABLE IF NOT EXISTS healing_history (
        healing_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_id      TEXT NOT NULL,
        step_name         TEXT NOT NULL,
        error_hash        TEXT NOT NULL,
        diagnosis_category TEXT NOT NULL,
        fix_applied       TEXT NOT NULL,
        success           INTEGER NOT NULL,
        timestamp         REAL NOT NULL,
        FOREIGN KEY (migration_id) REFERENCES migrations(migration_id)
    );
    
    -- AI Memory (error patterns)
    CREATE TABLE IF NOT EXISTS ai_memory (
        error_hash        TEXT PRIMARY KEY,
        error_signature   TEXT NOT NULL,
        fix_commands      TEXT NOT NULL,
        success_count     INTEGER DEFAULT 0,
        failure_count     INTEGER DEFAULT 0,
        os_version        TEXT DEFAULT '',
        python_version    TEXT DEFAULT '',
        package_versions  TEXT DEFAULT '',
        command_failed    TEXT,
        step_context      TEXT,
        first_seen        REAL NOT NULL,
        last_seen         REAL NOT NULL,
        last_successful_fix REAL DEFAULT 0
    );
    
    -- Locks (distributed)
    CREATE TABLE IF NOT EXISTS locks (
        lock_key          TEXT PRIMARY KEY,
        owner_id          TEXT NOT NULL,
        expires_at        REAL NOT NULL,
        created_at        REAL NOT NULL
    );
    
    -- Indexes
    CREATE INDEX IF NOT EXISTS idx_steps_migration ON migration_steps(migration_id);
    CREATE INDEX IF NOT EXISTS idx_steps_status ON migration_steps(migration_id, status);
    CREATE INDEX IF NOT EXISTS idx_journal_migration ON migration_journal(migration_id);
    CREATE INDEX IF NOT EXISTS idx_journal_timestamp ON migration_journal(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_events_migration ON events(migration_id);
    CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC);
    CREATE INDEX IF NOT EXISTS idx_snapshots_migration ON resource_snapshots(migration_id);
    CREATE INDEX IF NOT EXISTS idx_healing_migration ON healing_history(migration_id);
    CREATE INDEX IF NOT EXISTS idx_locks_expires ON locks(expires_at);
    """
    
    def __init__(self, db_path: str = "migration_state.db"):
        self.db_path = str(Path(db_path).expanduser())
        self._lock = threading.RLock()
        self._local = threading.local()
        self._initialize_database()
        logger.info(f"StateManager initialized: {self.db_path}")
    
    def _get_connection(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=10000")
            self._local.conn = conn
        return self._local.conn
    
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._get_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    
    def _initialize_database(self) -> None:
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO schema_info (version) VALUES (?)",
                (self.SCHEMA_VERSION,)
            )
    
    # ------------------------------------------------------------------ #
    # Migration Records
    # ------------------------------------------------------------------ #
    
    def create_migration(
        self,
        migration_id: str,
        bot_name: str,
        source_vps: str,
        destination_vps: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> MigrationRecord:
        now = time.time()
        
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO migrations (
                    migration_id, bot_name, source_vps, destination_vps,
                    status, started_at, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    migration_id,
                    bot_name,
                    source_vps,
                    destination_vps,
                    MigrationStatus.SCHEDULED.value,
                    now,
                    json.dumps(metadata) if metadata else None
                )
            )
        
        logger.info(f"Created migration: {migration_id}")
        return self.get_migration(migration_id)
    
    def get_migration(self, migration_id: str) -> Optional[MigrationRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM migrations WHERE migration_id = ?",
                (migration_id,)
            ).fetchone()
        
        if not row:
            return None
        
        return self._row_to_migration(row)
    
    def list_migrations(self, limit: int = 10) -> List[MigrationRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM migrations ORDER BY started_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
        
        return [self._row_to_migration(row) for row in rows]
    
    def _row_to_migration(self, row) -> MigrationRecord:
        return MigrationRecord(
            migration_id=row["migration_id"],
            bot_name=row["bot_name"],
            source_vps=row["source_vps"],
            destination_vps=row["destination_vps"],
            status=MigrationStatus(row["status"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            total_steps=row["total_steps"] or 0,
            completed_steps=row["completed_steps"] or 0,
            failed_steps=row["failed_steps"] or 0,
            healed_steps=row["healed_steps"] or 0,
            rollback_count=row["rollback_count"] or 0,
            metadata=row["metadata"],
        )
    
    def update_migration_status(
        self,
        migration_id: str,
        status: MigrationStatus,
        completed_at: Optional[float] = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE migrations
                SET status = ?, completed_at = ?
                WHERE migration_id = ?
                """,
                (status.value, completed_at or time.time(), migration_id)
            )
    
    def update_migration_stats(
        self,
        migration_id: str,
        total_steps: Optional[int] = None,
        completed_steps: Optional[int] = None,
        failed_steps: Optional[int] = None,
        healed_steps: Optional[int] = None
    ) -> None:
        updates = []
        params = []
        
        if total_steps is not None:
            updates.append("total_steps = ?")
            params.append(total_steps)
        if completed_steps is not None:
            updates.append("completed_steps = ?")
            params.append(completed_steps)
        if failed_steps is not None:
            updates.append("failed_steps = ?")
            params.append(failed_steps)
        if healed_steps is not None:
            updates.append("healed_steps = ?")
            params.append(healed_steps)
        
        if not updates:
            return
        
        params.append(migration_id)
        query = f"UPDATE migrations SET {', '.join(updates)} WHERE migration_id = ?"
        
        with self._connect() as conn:
            conn.execute(query, params)
    
    # ------------------------------------------------------------------ #
    # Steps
    # ------------------------------------------------------------------ #
    
    def create_step(
        self,
        migration_id: str,
        step_name: str,
        command: str,
        source_vps: str,
        destination_vps: str,
        checkpoint_id: Optional[str] = None
    ) -> int:
        now = time.time()
        
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO migration_steps (
                    migration_id, step_name, command, status,
                    source_vps, destination_vps, checkpoint_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    migration_id,
                    step_name,
                    command,
                    StepStatus.PENDING.value,
                    source_vps,
                    destination_vps,
                    checkpoint_id,
                    now,
                    now
                )
            )
            step_id = cursor.lastrowid
        
        self.update_migration_stats(migration_id, total_steps=self._count_steps(migration_id))
        logger.info(f"Created step {step_id}: {step_name}")
        return step_id
    
    def update_step(
        self,
        step_id: int,
        status: StepStatus,
        stdout: str = "",
        stderr: str = "",
        exit_code: Optional[int] = None,
        error_message: str = "",
        error_hash: Optional[str] = None,
        checkpoint_id: Optional[str] = None,
        increment_retry: bool = False
    ) -> None:
        with self._connect() as conn:
            if increment_retry:
                conn.execute(
                    """
                    UPDATE migration_steps
                    SET status = ?, last_stdout = ?, last_stderr = ?,
                        exit_code = ?, error_message = ?, error_hash = ?,
                        checkpoint_id = COALESCE(?, checkpoint_id),
                        retries = retries + 1, updated_at = ?
                    WHERE step_id = ?
                    """,
                    (
                        status.value, stdout, stderr, exit_code,
                        error_message, error_hash, checkpoint_id,
                        time.time(), step_id
                    )
                )
            else:
                conn.execute(
                    """
                    UPDATE migration_steps
                    SET status = ?, last_stdout = ?, last_stderr = ?,
                        exit_code = ?, error_message = ?, error_hash = ?,
                        checkpoint_id = COALESCE(?, checkpoint_id),
                        updated_at = ?
                    WHERE step_id = ?
                    """,
                    (
                        status.value, stdout, stderr, exit_code,
                        error_message, error_hash, checkpoint_id,
                        time.time(), step_id
                    )
                )
        
        self._update_step_stats_for_migration(step_id)
    
    def get_step(self, step_id: int) -> Optional[StepState]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM migration_steps WHERE step_id = ?",
                (step_id,)
            ).fetchone()
        
        if not row:
            return None
        
        return self._row_to_step(row)
    
    def get_steps(self, migration_id: str) -> List[StepState]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM migration_steps WHERE migration_id = ? ORDER BY step_id",
                (migration_id,)
            ).fetchall()
        
        return [self._row_to_step(row) for row in rows]
    
    def get_last_incomplete_step(self, migration_id: str) -> Optional[StepState]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM migration_steps
                WHERE migration_id = ?
                  AND status NOT IN (?, ?, ?, ?)
                ORDER BY step_id DESC
                LIMIT 1
                """,
                (
                    migration_id,
                    StepStatus.SUCCEEDED.value,
                    StepStatus.ROLLED_BACK.value,
                    StepStatus.SKIPPED.value,
                    StepStatus.CHECKPOINT.value
                )
            ).fetchone()
        
        return self._row_to_step(row) if row else None
    
    def _row_to_step(self, row: sqlite3.Row) -> StepState:
        return StepState(
            step_id=row["step_id"],
            migration_id=row["migration_id"],
            step_name=row["step_name"],
            command=row["command"],
            status=StepStatus(row["status"]),
            source_vps=row["source_vps"],
            destination_vps=row["destination_vps"],
            retries=row["retries"],
            last_stdout=row["last_stdout"],
            last_stderr=row["last_stderr"],
            exit_code=row["exit_code"],
            error_message=row["error_message"],
            error_hash=row["error_hash"],
            checkpoint_id=row["checkpoint_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"]
        )
    
    def _count_steps(self, migration_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM migration_steps WHERE migration_id = ?",
                (migration_id,)
            ).fetchone()
        return row[0] if row else 0
    
    def _update_step_stats_for_migration(self, step_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT migration_id, status FROM migration_steps WHERE step_id = ?",
                (step_id,)
            ).fetchone()
            
            if not row:
                return
            
            migration_id = row["migration_id"]
            
            status_counts = conn.execute(
                """
                SELECT status, COUNT(*) as count
                FROM migration_steps
                WHERE migration_id = ?
                GROUP BY status
                """,
                (migration_id,)
            ).fetchall()
            
            completed = 0
            failed = 0
            healed = 0
            
            for sc in status_counts:
                if sc["status"] in (StepStatus.SUCCEEDED.value, StepStatus.HEALED.value, StepStatus.CHECKPOINT.value):
                    completed += sc["count"]
                elif sc["status"] == StepStatus.FAILED.value:
                    failed += sc["count"]
                elif sc["status"] == StepStatus.HEALED.value:
                    healed += sc["count"]
            
            self.update_migration_stats(
                migration_id,
                completed_steps=completed,
                failed_steps=failed,
                healed_steps=healed
            )
    
    # ------------------------------------------------------------------ #
    # Checkpoints
    # ------------------------------------------------------------------ #
    
    def create_checkpoint(
        self,
        migration_id: str,
        step_name: str,
        description: str,
        state_snapshot: Dict[str, Any],
        files_checksum: Optional[str] = None
    ) -> str:
        checkpoint_id = f"chk-{migration_id}-{step_name}-{int(time.time())}"
        
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, migration_id, step_name,
                    description, state_snapshot, files_checksum,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    migration_id,
                    step_name,
                    description,
                    json.dumps(state_snapshot),
                    files_checksum,
                    time.time()
                )
            )
        
        logger.info(f"Created checkpoint {checkpoint_id}")
        return checkpoint_id
    
    def get_checkpoint(self, checkpoint_id: str) -> Optional[Checkpoint]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,)
            ).fetchone()
        
        if not row:
            return None
        
        return Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            migration_id=row["migration_id"],
            step_name=row["step_name"],
            description=row["description"],
            state_snapshot=json.loads(row["state_snapshot"]),
            files_checksum=row["files_checksum"],
            created_at=row["created_at"],
            restored_at=row["restored_at"],
        )
    
    def get_checkpoints(self, migration_id: str) -> List[Checkpoint]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM checkpoints WHERE migration_id = ? ORDER BY created_at DESC",
                (migration_id,)
            ).fetchall()
        
        return [
            Checkpoint(
                checkpoint_id=row["checkpoint_id"],
                migration_id=row["migration_id"],
                step_name=row["step_name"],
                description=row["description"],
                state_snapshot=json.loads(row["state_snapshot"]),
                files_checksum=row["files_checksum"],
                created_at=row["created_at"],
                restored_at=row["restored_at"],
            )
            for row in rows
        ]
    
    def restore_checkpoint(self, checkpoint_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE checkpoints SET restored_at = ? WHERE checkpoint_id = ?",
                (time.time(), checkpoint_id)
            )
    
    # ------------------------------------------------------------------ #
    # Journal
    # ------------------------------------------------------------------ #
    
    def add_journal_entry(
        self,
        migration_id: str,
        message: str,
        level: str = "INFO",
        step_name: Optional[str] = None,
        data: Optional[Dict[str, Any]] = None
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO migration_journal (
                    migration_id, timestamp, level, step_name, message, data
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    migration_id,
                    time.time(),
                    level,
                    step_name,
                    message,
                    json.dumps(data) if data else None
                )
            )
            return cursor.lastrowid
    
    def get_journal(self, migration_id: str, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM migration_journal
                WHERE migration_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (migration_id, limit)
            ).fetchall()
        
        return [
            {
                "entry_id": row["entry_id"],
                "timestamp": row["timestamp"],
                "level": row["level"],
                "step_name": row["step_name"],
                "message": row["message"],
                "data": json.loads(row["data"]) if row["data"] else None
            }
            for row in rows
        ]
    
    def get_journal_since(self, migration_id: str, since_timestamp: float) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM migration_journal
                WHERE migration_id = ? AND timestamp >= ?
                ORDER BY timestamp ASC
                """,
                (migration_id, since_timestamp)
            ).fetchall()
        
        return [
            {
                "entry_id": row["entry_id"],
                "timestamp": row["timestamp"],
                "level": row["level"],
                "step_name": row["step_name"],
                "message": row["message"],
                "data": json.loads(row["data"]) if row["data"] else None
            }
            for row in rows
        ]
    
    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    
    def store_event(
        self,
        event_id: str,
        migration_id: str,
        event_type: str,
        source: str,
        category: str,
        data: Optional[Dict[str, Any]] = None,
        correlation_id: Optional[str] = None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO events (
                    event_id, migration_id, event_type, data,
                    source, category, timestamp, correlation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    migration_id,
                    event_type,
                    json.dumps(data) if data else None,
                    source,
                    category,
                    time.time(),
                    correlation_id
                )
            )
    
    def get_events(self, migration_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM events
                WHERE migration_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (migration_id, limit)
            ).fetchall()
        
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "data": json.loads(row["data"]) if row["data"] else None,
                "source": row["source"],
                "category": row["category"],
                "timestamp": row["timestamp"],
                "correlation_id": row["correlation_id"],
            }
            for row in rows
        ]
    
    # ------------------------------------------------------------------ #
    # Resource Snapshots
    # ------------------------------------------------------------------ #
    
    def add_resource_snapshot(
        self,
        migration_id: str,
        cpu_percent: float,
        ram_percent: float,
        disk_percent: float,
        process_count: int,
        details: Optional[Dict[str, Any]] = None
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO resource_snapshots (
                    migration_id, timestamp, cpu_percent, ram_percent,
                    disk_percent, process_count, details
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    migration_id,
                    time.time(),
                    cpu_percent,
                    ram_percent,
                    disk_percent,
                    process_count,
                    json.dumps(details) if details else None
                )
            )
            return cursor.lastrowid
    
    def get_resource_snapshots(self, migration_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM resource_snapshots
                WHERE migration_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (migration_id, limit)
            ).fetchall()
        
        return [
            {
                "snapshot_id": row["snapshot_id"],
                "timestamp": row["timestamp"],
                "cpu_percent": row["cpu_percent"],
                "ram_percent": row["ram_percent"],
                "disk_percent": row["disk_percent"],
                "process_count": row["process_count"],
                "details": json.loads(row["details"]) if row["details"] else None,
            }
            for row in rows
        ]
    
    # ------------------------------------------------------------------ #
    # Healing History
    # ------------------------------------------------------------------ #
    
    def add_healing_history(
        self,
        migration_id: str,
        step_name: str,
        error_hash: str,
        diagnosis_category: str,
        fix_applied: str,
        success: bool
    ) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO healing_history (
                    migration_id, step_name, error_hash,
                    diagnosis_category, fix_applied, success, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    migration_id,
                    step_name,
                    error_hash,
                    diagnosis_category,
                    fix_applied,
                    1 if success else 0,
                    time.time()
                )
            )
            return cursor.lastrowid
    
    def get_healing_history(self, migration_id: str, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM healing_history
                WHERE migration_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (migration_id, limit)
            ).fetchall()
        
        return [
            {
                "healing_id": row["healing_id"],
                "step_name": row["step_name"],
                "error_hash": row["error_hash"],
                "diagnosis_category": row["diagnosis_category"],
                "fix_applied": row["fix_applied"],
                "success": bool(row["success"]),
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]
    
    # ------------------------------------------------------------------ #
    # AI Memory (Error Patterns)
    # ------------------------------------------------------------------ #
    
    def save_error_memory(self, memory: ErrorMemory) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO ai_memory (
                    error_hash, error_signature, fix_commands,
                    success_count, failure_count, os_version,
                    python_version, package_versions, command_failed,
                    step_context, first_seen, last_seen, last_successful_fix
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory.error_hash,
                    memory.error_signature,
                    json.dumps(memory.fix_commands),
                    memory.success_count,
                    memory.failure_count,
                    memory.os_version,
                    memory.python_version,
                    memory.package_versions,
                    memory.command_failed,
                    memory.step_context,
                    memory.first_seen,
                    memory.last_seen,
                    memory.last_successful_fix
                )
            )
    
    def get_error_memory(self, error_hash: str) -> Optional[ErrorMemory]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_memory WHERE error_hash = ?",
                (error_hash,)
            ).fetchone()
        
        if not row:
            return None
        
        return ErrorMemory(
            error_hash=row["error_hash"],
            error_signature=row["error_signature"],
            fix_commands=json.loads(row["fix_commands"]),
            success_count=row["success_count"],
            failure_count=row["failure_count"],
            os_version=row["os_version"],
            python_version=row["python_version"],
            package_versions=row["package_versions"],
            command_failed=row["command_failed"],
            step_context=row["step_context"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            last_successful_fix=row["last_successful_fix"]
        )
    
    def get_best_fix(self, error_hash: str) -> Optional[Dict[str, Any]]:
        memory = self.get_error_memory(error_hash)
        if not memory or memory.success_rate < 0.5:
            return None
        
        return {
            "fix_commands": memory.fix_commands,
            "success_rate": memory.success_rate,
            "times_used": memory.success_count + memory.failure_count,
            "last_successful": memory.last_successful_fix
        }
    
    def record_fix_result(self, error_hash: str, success: bool) -> None:
        with self._connect() as conn:
            if success:
                conn.execute(
                    """
                    UPDATE ai_memory
                    SET success_count = success_count + 1,
                        last_successful_fix = ?,
                        last_seen = ?
                    WHERE error_hash = ?
                    """,
                    (time.time(), time.time(), error_hash)
                )
            else:
                conn.execute(
                    """
                    UPDATE ai_memory
                    SET failure_count = failure_count + 1,
                        last_seen = ?
                    WHERE error_hash = ?
                    """,
                    (time.time(), error_hash)
                )
    
    # ------------------------------------------------------------------ #
    # Locks
    # ------------------------------------------------------------------ #
    
    def acquire_lock(self, lock_key: str, owner_id: str, ttl_seconds: float = 60.0) -> bool:
        now = time.time()
        expires_at = now + ttl_seconds
        
        with self._connect() as conn:
            conn.execute("DELETE FROM locks WHERE expires_at < ?", (now,))
            
            try:
                conn.execute(
                    """
                    INSERT INTO locks (lock_key, owner_id, expires_at, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (lock_key, owner_id, expires_at, now)
                )
                return True
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT owner_id FROM locks WHERE lock_key = ?",
                    (lock_key,)
                ).fetchone()
                
                if row and row["owner_id"] == owner_id:
                    conn.execute(
                        "UPDATE locks SET expires_at = ? WHERE lock_key = ?",
                        (expires_at, lock_key)
                    )
                    return True
                
                return False
    
    def release_lock(self, lock_key: str, owner_id: str) -> bool:
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM locks WHERE lock_key = ? AND owner_id = ?",
                (lock_key, owner_id)
            )
            return result.rowcount > 0
    
    def renew_lock(self, lock_key: str, owner_id: str, ttl_seconds: float = 60.0) -> bool:
        now = time.time()
        expires_at = now + ttl_seconds
        
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE locks SET expires_at = ? WHERE lock_key = ? AND owner_id = ?",
                (expires_at, lock_key, owner_id)
            )
            return result.rowcount > 0
    
    # ------------------------------------------------------------------ #
    # Timeline
    # ------------------------------------------------------------------ #
    
    def get_timeline(self, migration_id: str) -> List[Dict[str, Any]]:
        """Get complete migration timeline from all sources."""
        timeline = []
        
        # Get journal entries
        journal = self.get_journal(migration_id, limit=500)
        for entry in journal:
            timeline.append({
                "timestamp": entry["timestamp"],
                "type": "journal",
                "level": entry["level"],
                "message": entry["message"],
                "step_name": entry["step_name"],
            })
        
        # Get events
        events = self.get_events(migration_id, limit=500)
        for event in events:
            timeline.append({
                "timestamp": event["timestamp"],
                "type": "event",
                "event_type": event["event_type"],
                "source": event["source"],
                "data": event["data"],
            })
        
        # Sort by timestamp
        timeline.sort(key=lambda x: x["timestamp"])
        
        return timeline
    
    # ------------------------------------------------------------------ #
    # Cleanup
    # ------------------------------------------------------------------ #
    
    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None
        logger.info("StateManager closed")
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
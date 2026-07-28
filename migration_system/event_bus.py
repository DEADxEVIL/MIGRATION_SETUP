from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Union

logger = logging.getLogger("event_bus")


class EventPriority(str, Enum):
    """Event priority levels."""
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"
    DEBUG = "debug"


class EventCategory(str, Enum):
    """Event categories."""
    MIGRATION = "migration"
    SSH = "ssh"
    HEALTH = "health"
    HEALING = "healing"
    ROLLBACK = "rollback"
    SYSTEM = "system"
    COMMAND = "command"
    CHECKPOINT = "checkpoint"
    STATE = "state"


@dataclass
class Event:
    """Immutable event."""
    event_type: str
    data: Dict[str, Any]
    source: str
    category: EventCategory = EventCategory.SYSTEM
    priority: EventPriority = EventPriority.NORMAL
    timestamp: float = field(default_factory=time.time)
    correlation_id: Optional[str] = None
    event_id: str = field(default_factory=lambda: f"evt-{uuid.uuid4().hex[:12]}")
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "data": self.data,
            "source": self.source,
            "category": self.category.value,
            "priority": self.priority.value,
            "timestamp": self.timestamp,
            "correlation_id": self.correlation_id,
        }
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


Subscriber = Callable[[Event], None]


class EventBus:
    """
    PURE EVENT BROADCASTER.
    
    Only broadcasts events. Does NOT control anything.
    """
    
    def __init__(self, state_manager_ref: Any = None, max_history: int = 1000):
        self._subscribers: Dict[str, List[Subscriber]] = {}
        self._lock = threading.RLock()
        self._history: List[Event] = []
        self._max_history = max_history
        self._state_manager = state_manager_ref
        self._dead_letter: List[Event] = []
        self._enabled = True
    
    def set_state_manager(self, state_manager: Any) -> None:
        """Set StateManager reference for persistence."""
        self._state_manager = state_manager
    
    def subscribe(self, event_pattern: str, callback: Subscriber) -> None:
        """
        Subscribe to events matching a pattern.
        
        Patterns:
        - "STEP_*" - matches STEP_STARTED, STEP_COMPLETED, etc.
        - "*.FAILED" - matches STEP_FAILED, COMMAND_FAILED, etc.
        - "*" - matches all events
        - Exact match: "STEP_STARTED"
        """
        with self._lock:
            if event_pattern not in self._subscribers:
                self._subscribers[event_pattern] = []
            self._subscribers[event_pattern].append(callback)
            logger.debug("Subscribed %s to pattern '%s'", 
                        getattr(callback, '__name__', str(callback)), 
                        event_pattern)
    
    def unsubscribe(self, event_pattern: str, callback: Subscriber) -> bool:
        """Remove a subscriber."""
        with self._lock:
            if event_pattern not in self._subscribers:
                return False
            try:
                self._subscribers[event_pattern].remove(callback)
                logger.debug("Unsubscribed from '%s'", event_pattern)
                return True
            except ValueError:
                return False
    
    def publish(self, event: Event) -> None:
        """
        Broadcast an event to all matching subscribers.
        
        Also persists event to StateManager if available.
        """
        if not self._enabled:
            logger.debug("Event bus disabled, dropping event: %s", event.event_type)
            return
        
        # Store in history
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
        
        # Persist to StateManager
        if self._state_manager:
            try:
                self._state_manager.store_event(
                    event_id=event.event_id,
                    migration_id=event.correlation_id or "",
                    event_type=event.event_type,
                    source=event.source,
                    category=event.category.value,
                    data=event.data,
                    correlation_id=event.correlation_id,
                )
            except Exception as e:
                logger.error("Failed to persist event: %s", e)
        
        logger.debug("Event: %s (source=%s)", event.event_type, event.source)
        
        # Find and dispatch to subscribers
        subscribers = self._get_matching_subscribers(event.event_type)
        if not subscribers:
            return
        
        for callback in subscribers:
            try:
                callback(event)
            except Exception as e:
                logger.error("Subscriber %s error: %s", 
                            getattr(callback, '__name__', str(callback)), e)
                self._dead_letter.append(event)
                if len(self._dead_letter) > 100:
                    self._dead_letter = self._dead_letter[-100:]
    
    def _get_matching_subscribers(self, event_type: str) -> List[Subscriber]:
        """Get all subscribers matching an event type."""
        with self._lock:
            result = []
            
            # Exact match
            if event_type in self._subscribers:
                result.extend(self._subscribers[event_type])
            
            # Wildcards
            for pattern, subs in self._subscribers.items():
                if pattern == "*":
                    result.extend(subs)
                elif pattern.endswith("*"):
                    prefix = pattern[:-1]
                    if event_type.startswith(prefix):
                        result.extend(subs)
                elif pattern.startswith("*"):
                    suffix = pattern[1:]
                    if event_type.endswith(suffix):
                        result.extend(subs)
            
            # Remove duplicates
            seen = set()
            unique = []
            for sub in result:
                if id(sub) not in seen:
                    seen.add(id(sub))
                    unique.append(sub)
            
            return unique
    
    def get_history(self, event_type: Optional[str] = None, limit: int = 100) -> List[Event]:
        """Get event history."""
        with self._lock:
            if event_type:
                return [e for e in self._history[-limit:] if e.event_type == event_type]
            return self._history[-limit:]
    
    def get_dead_letter(self) -> List[Event]:
        with self._lock:
            return list(self._dead_letter)
    
    def clear_history(self) -> None:
        with self._lock:
            self._history.clear()
    
    def enable(self) -> None:
        self._enabled = True
    
    def disable(self) -> None:
        self._enabled = False
    
    def shutdown(self) -> None:
        self._enabled = False
        with self._lock:
            self._subscribers.clear()
        logger.info("Event bus shut down")


# Event type constants
EVENT_TYPES = {
    "STEP_STARTED": "STEP_STARTED",
    "STEP_COMPLETED": "STEP_COMPLETED",
    "STEP_FAILED": "STEP_FAILED",
    "STEP_RETRYING": "STEP_RETRYING",
    "BOT_STARTED": "BOT_STARTED",
    "BOT_STOPPED": "BOT_STOPPED",
    "ROLLBACK_STARTED": "ROLLBACK_STARTED",
    "ROLLBACK_COMPLETED": "ROLLBACK_COMPLETED",
    "HEALING_STARTED": "HEALING_STARTED",
    "HEALING_COMPLETED": "HEALING_COMPLETED",
    "HEALING_FAILED": "HEALING_FAILED",
    "CHECKPOINT_CREATED": "CHECKPOINT_CREATED",
    "CHECKPOINT_RESTORED": "CHECKPOINT_RESTORED",
    "MIGRATION_STARTED": "MIGRATION_STARTED",
    "MIGRATION_COMPLETED": "MIGRATION_COMPLETED",
    "MIGRATION_FAILED": "MIGRATION_FAILED",
    "MIGRATION_CANCELLED": "MIGRATION_CANCELLED",
    "SSH_CONNECTED": "SSH_CONNECTED",
    "SSH_DISCONNECTED": "SSH_DISCONNECTED",
    "SSH_COMMAND_STARTED": "SSH_COMMAND_STARTED",
    "SSH_COMMAND_COMPLETED": "SSH_COMMAND_COMPLETED",
    "SSH_COMMAND_FAILED": "SSH_COMMAND_FAILED",
    "HEALTH_CHECK_STARTED": "HEALTH_CHECK_STARTED",
    "HEALTH_CHECK_PASSED": "HEALTH_CHECK_PASSED",
    "HEALTH_CHECK_FAILED": "HEALTH_CHECK_FAILED",
    "COMMAND_STARTED": "COMMAND_STARTED",
    "COMMAND_COMPLETED": "COMMAND_COMPLETED",
    "COMMAND_FAILED": "COMMAND_FAILED",
    "LOCK_ACQUIRED": "LOCK_ACQUIRED",
    "LOCK_RELEASED": "LOCK_RELEASED",
}
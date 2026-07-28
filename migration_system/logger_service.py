from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

# Global state
_lock = threading.RLock()
_configured_loggers: Set[str] = set()
_log_subscribers: List[Callable[[str, str, str], None]] = []
_root_configured = False

# Defaults
DEFAULT_LOG_DIR = Path("/var/log/migration_system") if Path("/var/log").exists() else Path("./logs")
DEFAULT_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(threadName)s | %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class ColoredFormatter(logging.Formatter):
    """Formatter with ANSI color codes for console output."""
    
    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[41m',  # Red background
    }
    RESET = '\033[0m'
    
    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, '')
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """JSON formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "thread": record.threadName,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        if hasattr(record, "extra_data"):
            log_entry["extra"] = record.extra_data
            
        return json.dumps(log_entry)


class LogSubscriberHandler(logging.Handler):
    """Handler that forwards logs to registered subscribers."""
    
    def __init__(self):
        super().__init__()
        self._subscribers: List[Callable[[str, str, str], None]] = []
        self._lock = threading.RLock()
    
    def add_subscriber(self, callback: Callable[[str, str, str], None]) -> None:
        """Add a subscriber callback (logger_name, level, message)."""
        with self._lock:
            self._subscribers.append(callback)
    
    def remove_subscriber(self, callback: Callable[[str, str, str], None]) -> None:
        """Remove a subscriber callback."""
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)
    
    def emit(self, record: logging.LogRecord) -> None:
        """Forward log record to all subscribers."""
        if not self._subscribers:
            return
            
        message = self.format(record)
        with self._lock:
            subscribers = list(self._subscribers)
        
        for callback in subscribers:
            try:
                callback(record.name, record.levelname, message)
            except Exception:
                # Don't let subscriber errors break logging
                pass


# Global subscriber handler instance
_subscriber_handler = LogSubscriberHandler()


def configure_root_logging(
    log_dir: Optional[str] = None,
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
    enable_json: bool = False,
) -> None:
    """Configure the root logger once for the whole process."""
    global _root_configured
    
    with _lock:
        if _root_configured:
            return
            
        directory = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "migration_system.log"
        
        root = logging.getLogger()
        root.setLevel(level)
        
        # Console handler with colors
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(ColoredFormatter(DEFAULT_LOG_FORMAT, DEFAULT_DATE_FORMAT))
        root.addHandler(console_handler)
        
        # File handler with rotation
        file_handler = logging.handlers.RotatingFileHandler(
            str(log_path), maxBytes=max_bytes, backupCount=backup_count
        )
        file_formatter = JsonFormatter() if enable_json else logging.Formatter(DEFAULT_LOG_FORMAT, DEFAULT_DATE_FORMAT)
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)
        
        # Subscriber handler (always added)
        _subscriber_handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT, DEFAULT_DATE_FORMAT))
        root.addHandler(_subscriber_handler)
        
        _root_configured = True
        
        # Log startup
        root.info(f"Logging initialized. Log file: {log_path}")


def get_logger(name: str, level: Optional[int] = None) -> logging.Logger:
    """Get a configured logger instance."""
    configure_root_logging()
    
    with _lock:
        logger = logging.getLogger(name)
        if level is not None:
            logger.setLevel(level)
        _configured_loggers.add(name)
        return logger


def add_log_subscriber(callback: Callable[[str, str, str], None]) -> None:
    """Add a callback for all log messages (logger_name, level, message)."""
    _subscriber_handler.add_subscriber(callback)


def remove_log_subscriber(callback: Callable[[str, str, str], None]) -> None:
    """Remove a log subscriber callback."""
    _subscriber_handler.remove_subscriber(callback)


def set_global_level(level: int) -> None:
    """Change log level for all configured loggers."""
    with _lock:
        logging.getLogger().setLevel(level)
        for name in _configured_loggers:
            logging.getLogger(name).setLevel(level)


# Test function
if __name__ == "__main__":
    # Test logging
    configure_root_logging(enable_json=False)
    logger = get_logger("test")
    
    def test_subscriber(name: str, level: str, message: str) -> None:
        print(f"[SUBSCRIBER] {name} | {level}: {message}")
    
    add_log_subscriber(test_subscriber)
    
    logger.debug("Debug message")
    logger.info("Info message")
    logger.warning("Warning message")
    logger.error("Error message")
    
    # Test JSON logging
    configure_root_logging(enable_json=True)
    json_logger = get_logger("json_test")
    json_logger.info("JSON formatted log")
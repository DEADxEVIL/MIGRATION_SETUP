from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, List, Optional, Tuple, Type, TypeVar, Union

logger = logging.getLogger("retry_engine")


class BackoffStrategy(str, Enum):
    """Backoff strategies for retry delays."""
    FIXED = "fixed"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    EXPONENTIAL_JITTER = "exponential_jitter"


@dataclass
class RetryPolicy:
    """Configuration for retry behavior."""
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    
    def delay_for_attempt(self, attempt: int) -> float:
        """Calculate delay for a given attempt number (1-indexed)."""
        if self.strategy == BackoffStrategy.FIXED:
            delay = self.base_delay_seconds
        elif self.strategy == BackoffStrategy.LINEAR:
            delay = self.base_delay_seconds * attempt
        elif self.strategy == BackoffStrategy.EXPONENTIAL:
            delay = self.base_delay_seconds * (2 ** (attempt - 1))
        elif self.strategy == BackoffStrategy.EXPONENTIAL_JITTER:
            exponential = self.base_delay_seconds * (2 ** (attempt - 1))
            delay = random.uniform(0, exponential)
        else:
            delay = self.base_delay_seconds
        
        return min(delay, self.max_delay_seconds)


T = TypeVar('T')


class RetryExhaustedError(Exception):
    """Raised when all retry attempts are exhausted."""
    
    def __init__(self, attempts: int, last_exception: Optional[Exception] = None):
        self.attempts = attempts
        self.last_exception = last_exception
        message = f"Retry exhausted after {attempts} attempt(s)"
        if last_exception:
            message += f": {last_exception}"
        super().__init__(message)


def retry_call(
    func: Callable[[], T],
    policy: Optional[RetryPolicy] = None,
    retry_on: Tuple[Type[Exception], ...] = (Exception,),
    should_retry_result: Optional[Callable[[T], bool]] = None,
    on_attempt: Optional[Callable[[int, Optional[Exception], Optional[T]], None]] = None,
    on_failure: Optional[Callable[[Exception], None]] = None,
    raise_on_exhaustion: bool = True
) -> T:
    """
    Execute a function with retries according to the given policy.
    
    Args:
        func: Callable to execute
        policy: Retry configuration
        retry_on: Exception types that trigger a retry
        should_retry_result: Predicate on result to trigger retry
        on_attempt: Callback on each attempt (attempt_number, exception, result)
        on_failure: Callback on each failure (exception)
        raise_on_exhaustion: If False, return None on exhaustion
    
    Returns:
        Result of func on success
    
    Raises:
        RetryExhaustedError: If all attempts fail and raise_on_exhaustion is True
    """
    active_policy = policy or RetryPolicy()
    last_exception: Optional[Exception] = None
    last_result: Optional[T] = None
    
    for attempt in range(1, active_policy.max_attempts + 1):
        try:
            result = func()
            last_result = result
            
            # Check if result should trigger retry
            if should_retry_result and should_retry_result(result):
                logger.warning(
                    "Attempt %d/%d returned result flagged for retry",
                    attempt, active_policy.max_attempts
                )
                if attempt == active_policy.max_attempts:
                    break
                
                if on_attempt:
                    on_attempt(attempt, None, result)
                
                delay = active_policy.delay_for_attempt(attempt)
                time.sleep(delay)
                continue
            
            # Success
            if on_attempt:
                on_attempt(attempt, None, result)
            return result
            
        except retry_on as e:  # type: ignore[misc]
            last_exception = e
            logger.warning(
                "Attempt %d/%d failed with %s: %s",
                attempt, active_policy.max_attempts,
                type(e).__name__, str(e)
            )
            
            if on_failure:
                on_failure(e)
            
            if attempt == active_policy.max_attempts:
                break
            
            if on_attempt:
                on_attempt(attempt, e, None)
            
            delay = active_policy.delay_for_attempt(attempt)
            time.sleep(delay)
    
    # Exhausted
    if raise_on_exhaustion:
        raise RetryExhaustedError(active_policy.max_attempts, last_exception)
    
    return last_result


class RetryContext:
    """
    Context manager for retry operations.
    
    Example:
        with RetryContext(max_attempts=3) as retry:
            for attempt in retry:
                try:
                    result = do_something()
                    if retry.should_retry_result(result):
                        continue
                    break
                except Exception as e:
                    retry.record_failure(e)
    """
    
    def __init__(
        self,
        max_attempts: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    ):
        self.policy = RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=base_delay,
            max_delay_seconds=max_delay,
            strategy=strategy
        )
        self.attempt = 0
        self.last_exception: Optional[Exception] = None
        self.should_continue = True
    
    def __enter__(self) -> RetryContext:
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        pass
    
    def __iter__(self):
        self.attempt = 0
        self.should_continue = True
        return self
    
    def __next__(self) -> int:
        if not self.should_continue or self.attempt >= self.policy.max_attempts:
            raise StopIteration
        
        self.attempt += 1
        
        if self.attempt > 1:
            delay = self.policy.delay_for_attempt(self.attempt - 1)
            time.sleep(delay)
        
        return self.attempt
    
    def record_failure(self, exception: Exception) -> None:
        """Record a failure for the current attempt."""
        self.last_exception = exception
        logger.warning(
            "Attempt %d/%d failed: %s",
            self.attempt, self.policy.max_attempts,
            str(exception)
        )
    
    def should_retry_result(self, result: Any, predicate: Callable[[Any], bool]) -> bool:
        """Check if result should trigger retry."""
        if predicate(result):
            logger.warning(
                "Attempt %d/%d result flagged for retry",
                self.attempt, self.policy.max_attempts
            )
            return True
        return False
    
    def abort(self) -> None:
        """Abort retry loop."""
        self.should_continue = False
    
    def is_exhausted(self) -> bool:
        """Check if attempts are exhausted."""
        return self.attempt >= self.policy.max_attempts


# Convenience functions
def retry_with_backoff(
    func: Callable[[], T],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    strategy: str = "exponential"
) -> T:
    """Simple retry with backoff."""
    policy = RetryPolicy(
        max_attempts=max_attempts,
        base_delay_seconds=base_delay,
        strategy=BackoffStrategy(strategy)
    )
    return retry_call(func, policy)


def retry_on_exception(
    func: Callable[[], T],
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    max_attempts: int = 3,
    base_delay: float = 1.0
) -> T:
    """Retry only on specific exceptions."""
    policy = RetryPolicy(max_attempts=max_attempts, base_delay_seconds=base_delay)
    return retry_call(func, policy, retry_on=exceptions)


if __name__ == "__main__":
    from logger_service import get_logger
    
    logger = get_logger("test")
    
    # Test retry
    counter = 0
    
    def flaky_function() -> str:
        global counter
        counter += 1
        if counter < 3:
            raise ValueError(f"Attempt {counter} failed")
        return f"Success on attempt {counter}"
    
    # Retry with exponential backoff
    result = retry_call(
        flaky_function,
        RetryPolicy(max_attempts=5, base_delay_seconds=0.1),
        retry_on=(ValueError,)
    )
    print(f"Result: {result}")
    
    # Test context manager
    counter = 0
    
    with RetryContext(max_attempts=3, base_delay=0.1) as retry:
        for attempt in retry:
            try:
                counter += 1
                if counter < 3:
                    raise RuntimeError(f"Failure {counter}")
                print(f"Success on attempt {counter}")
                break
            except Exception as e:
                retry.record_failure(e)
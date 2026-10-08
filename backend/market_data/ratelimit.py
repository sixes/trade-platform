from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional


class TokenBucket:
    """Simple token bucket; `reserve()` returns how long the caller must wait before proceeding."""

    def __init__(self, rate_per_second: float, capacity: int):
        self.rate = max(rate_per_second, 0.1)
        self.capacity = max(capacity, 1)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def reserve(self) -> float:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            if self._tokens >= 1:
                self._tokens -= 1
                return 0.0
            wait = (1 - self._tokens) / self.rate
            self._tokens -= 1
            return wait


class SlidingWindowBudget:
    """Counts units (e.g. option contracts quoted) in a rolling window and says how long to wait for room."""

    def __init__(self, limit: int, window_seconds: float = 60.0):
        self.limit = max(1, int(limit))
        self.window = float(window_seconds)
        self._events: deque = deque()  # (monotonic time, units)
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        while self._events and self._events[0][0] <= now - self.window:
            self._events.popleft()

    def used(self) -> int:
        with self._lock:
            self._prune(time.monotonic())
            return sum(c for _, c in self._events)

    def reserve(self, units: int) -> float:
        """Consume `units` if they fit; otherwise return seconds until enough of the window has expired."""
        units = max(0, int(units))
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            used = sum(c for _, c in self._events)
            if used + units <= self.limit:
                if units:
                    self._events.append((now, units))
                return 0.0
            needed = used + units - self.limit
            freed = 0
            release = now
            for stamp, count in self._events:
                freed += count
                release = stamp + self.window
                if freed >= needed:
                    break
            return max(0.05, release - now)

    def penalize(self) -> None:
        """The server said the window is exhausted: treat it as full from now."""
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            self._events.append((now, self.limit))


class QuotaState:
    """Thread-safe view of API usage and any quota wait, exposed through /api/status."""

    def __init__(self):
        self._lock = threading.Lock()
        self.total_calls = 0
        self.calls_by_method: dict = {}
        self.rate_limited_events = 0
        self.waiting_reason: Optional[str] = None
        self.waiting_until: Optional[float] = None
        self.last_error: Optional[str] = None
        self.auth_error: Optional[str] = None
        self.connected = False
        self.last_call_at: Optional[float] = None
        self._recent_calls: list = []

    def record_call(self, method: str) -> None:
        with self._lock:
            now = time.time()
            self.total_calls += 1
            self.calls_by_method[method] = self.calls_by_method.get(method, 0) + 1
            self.last_call_at = now
            self._recent_calls.append(now)
            self._recent_calls = [t for t in self._recent_calls if now - t <= 60]

    def set_waiting(self, reason: str, seconds: float) -> None:
        with self._lock:
            self.waiting_reason = reason
            self.waiting_until = time.time() + seconds

    def clear_waiting(self) -> None:
        with self._lock:
            self.waiting_reason = None
            self.waiting_until = None

    def note_rate_limited(self) -> None:
        with self._lock:
            self.rate_limited_events += 1

    def set_error(self, message: Optional[str]) -> None:
        with self._lock:
            self.last_error = message

    def set_auth_error(self, message: Optional[str]) -> None:
        with self._lock:
            self.auth_error = message
            if message:
                self.connected = False

    def set_connected(self, value: bool) -> None:
        with self._lock:
            self.connected = value
            if value:
                self.auth_error = None

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            remaining = max(0.0, (self.waiting_until or now) - now) if self.waiting_until else 0.0
            return {
                "connected": self.connected,
                "auth_error": self.auth_error,
                "total_calls": self.total_calls,
                "calls_last_minute": len([t for t in self._recent_calls if now - t <= 60]),
                "calls_by_method": dict(self.calls_by_method),
                "rate_limited_events": self.rate_limited_events,
                "waiting": bool(self.waiting_reason) and remaining > 0,
                "waiting_reason": self.waiting_reason if remaining > 0 else None,
                "waiting_seconds": round(remaining, 1),
                "last_error": self.last_error,
            }

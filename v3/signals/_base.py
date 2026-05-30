"""Signal base — SignalResult dataclass + timeout decorator."""
from __future__ import annotations

import functools
import signal as _signal
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, TypeVar

T = TypeVar("T")


@dataclass
class SignalResult:
    """Unified return shape for every signal collector.

    status: ok | failed | skipped
    score: 0-100 numeric score (None if not computed)
    data: signal-specific structured payload
    error: short message when status == failed
    elapsed_ms: wall time
    """
    signal: str
    status: str = "ok"
    score: float | None = None
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    elapsed_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _TimeoutError(Exception):
    pass


def with_timeout(seconds: int) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Cross-platform thread-based timeout. Returns SignalResult.failed on timeout."""
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            box: dict[str, Any] = {}

            def target() -> None:
                try:
                    box["result"] = fn(*args, **kwargs)
                except Exception as e:
                    box["error"] = e

            t0 = time.time()
            th = threading.Thread(target=target, daemon=True)
            th.start()
            th.join(seconds)
            elapsed = int((time.time() - t0) * 1000)
            if th.is_alive():
                # Thread still running; we cannot kill it but we return failed.
                signal_name = getattr(fn, "_signal_name", fn.__name__)
                return SignalResult(
                    signal=signal_name,
                    status="failed",
                    error=f"timeout after {seconds}s",
                    elapsed_ms=elapsed,
                )
            if "error" in box:
                signal_name = getattr(fn, "_signal_name", fn.__name__)
                return SignalResult(
                    signal=signal_name,
                    status="failed",
                    error=f"{type(box['error']).__name__}: {box['error']}",
                    elapsed_ms=elapsed,
                )
            res = box.get("result")
            if isinstance(res, SignalResult) and res.elapsed_ms is None:
                res.elapsed_ms = elapsed
            return res

        return wrapper

    return deco

"""Per-run budgets and bounded, serialized workers for correction stages.

Workers cannot be force-killed. A timed-out request stops waiting, cancels queued
work and discards late results. This pool is not asyncio's default executor, so
asyncio.run() does not wait for a late worker during shutdown.
"""

import asyncio
import threading
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, TypeVar

from .contracts import StreamEmitter

_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rag-correction")
_SLOTS = threading.BoundedSemaphore(4)
_Result = TypeVar("_Result")


async def bounded_sync(
    operation: Callable[[], _Result],
    deadline: float | None,
    lock: Any = None,
    cancelled: threading.Event | None = None,
) -> _Result:
    """Offload with bounded dispatch, optional deadline and cancellation-aware locks.

    Ordinary resource adapters have no new timeout, but share this finite pool
    and never wait for a resource lock on the event loop. Cancelled lock waiters
    exit cooperatively without dispatching their operation later.
    """
    while not _SLOTS.acquire(blocking=False):
        remaining = deadline - monotonic() if deadline is not None else None
        if remaining is not None and remaining <= 0:
            raise TimeoutError
        await asyncio.sleep(min(0.005, remaining) if remaining is not None else 0.005)

    cancelled = cancelled if cancelled is not None else threading.Event()

    def check_wait() -> None:
        if cancelled.is_set() or (deadline is not None and deadline <= monotonic()):
            raise TimeoutError

    def work() -> _Result:
        acquired = False
        try:
            check_wait()
            if lock is not None:
                while not acquired:
                    check_wait()
                    remaining = deadline - monotonic() if deadline is not None else 0.01
                    acquired = lock.acquire(timeout=max(0.0, min(0.01, remaining)))
            check_wait()
            return operation()
        finally:
            if acquired:
                lock.release()

    try:
        future = _WORKERS.submit(work)
    except Exception:
        _SLOTS.release()
        raise
    future.add_done_callback(lambda _future: _SLOTS.release())
    try:
        return await asyncio.wait_for(
            asyncio.wrap_future(future),
            max(0.000001, deadline - monotonic()) if deadline is not None else None,
        )
    except (asyncio.CancelledError, TimeoutError):
        cancelled.set()
        future.cancel()
        raise


def normalized(query: str) -> str:
    """Compare queries by normalized Unicode, case and whitespace."""
    return " ".join(unicodedata.normalize("NFKC", query).casefold().split())


@dataclass
class AgentRuntime:
    """State private to one container execution; never owned by registry nodes."""

    question: str
    deadline: float
    max_queries: int
    node_id: str
    emit: StreamEmitter | None = None
    iteration: int = 0
    allow_repair: bool = True
    active: bool = True
    model_calls: int = 0
    seen: set[str] = field(default_factory=set)
    queries: list[str] = field(default_factory=list)
    candidates: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    child_trace: list = field(default_factory=list)
    stop_reason: str | None = None
    dispatched: set[tuple[str, str]] = field(default_factory=set)
    retrieved: list = field(default_factory=list)
    assessment: dict | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)

    def check_active(self) -> None:
        """Reject new heavy suboperations after cancellation or stage expiration."""
        if not self.active or self.cancelled.is_set() or monotonic() >= self.deadline:
            raise TimeoutError

    def progress(self, stage: str, stop_reason: str | None = None) -> None:
        if not self.active or self.emit is None:
            return
        data = {
            "node_id": self.node_id,
            "stage": stage,
            "iteration": self.iteration,
            "evidence": len(self.evidence),
        }
        if stop_reason is not None:
            data["stop_reason"] = stop_reason
        self.emit({"event": "node.progress", "data": data})

    def filter_queries(self, queries: list[str], record: bool = True) -> list[str]:
        """Clamp independently of edited graph configuration, preserving original."""
        chosen, local = [], set()
        for query in queries:
            clean = query.strip()
            key = normalized(clean)
            if (
                not 0 < len(clean) <= 256
                or key in local
                or (record and key in self.seen)
                or (not record and key not in {normalized(q) for q in self.queries})
            ):
                continue
            local.add(key)
            chosen.append(clean)
            if len(chosen) == self.max_queries:
                break
        if record:
            self.seen.update(local)
            self.queries = chosen
        return chosen

    def reserve_queries(self, queries: list[str], strategy: str) -> list[str]:
        """Allow each approved query at most one dispatch per retrieval strategy.

        Hybrid reserves both dense and BM25 legs atomically. Separate dense and
        BM25 branches remain valid, but duplicate edited retrieval instances do
        not multiply work beyond the stage's budget.
        """
        legs = ("dense", "bm25") if strategy == "hybrid" else (strategy,)
        chosen = []
        for query in queries:
            keys = {(normalized(query), leg) for leg in legs}
            if keys & self.dispatched:
                continue
            self.dispatched.update(keys)
            chosen.append(query)
        return chosen

"""Transport-neutral Server-Sent Events bridge for blocking workflow runs.

The producer is a plain function that runs in a worker thread; the consumer is an
async generator drained by the HTTP layer. Events are validated as independent
JSON snapshots before they are queued, so a producer that keeps and mutates its
dict after the call cannot change what was already sent.

Nothing here knows about retrieval, configuration or the model transport: the
event whitelist and the public error mapping are supplied by each caller.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable

from fastapi.responses import StreamingResponse

StreamEvent = dict[str, object]
StreamEmitter = Callable[[StreamEvent], None]

MAX_EVENT_DEPTH = 64


def event_frame(event: dict) -> str:
    """Encode one stream event as a Server-Sent Events frame."""
    return f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"


def event_snapshot(event: object, allowed_events: frozenset[str]) -> dict | None:
    """Return an independent JSON snapshot, or ``None`` when the event is invalid.

    The producer retains its own object and may mutate it after this call, so the
    round-trip is what makes the queued value immutable. Depth is bounded because
    the payload is produced by untrusted model output.
    """
    try:
        if type(event) is not dict:
            return None
        snapshot = json.loads(json.dumps(event, ensure_ascii=False, allow_nan=False))
        pending = [(snapshot, 0)]
        while pending:
            value, depth = pending.pop()
            if depth > MAX_EVENT_DEPTH:
                return None
            if isinstance(value, dict):
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
        name = snapshot.get("event")
        if type(name) is not str or name not in allowed_events:
            return None
        return snapshot
    except Exception:  # noqa: BLE001 - untrusted payload never reaches the client
        return None


def bridge(execute: Callable[[StreamEmitter], dict],
           allowed_events: frozenset[str],
           *,
           failure: Callable[[Exception], dict],
           frame: Callable[[dict], str] = event_frame) -> StreamingResponse:
    """Run ``execute`` in a worker thread and stream the events it emits.

    Args:
        execute: Blocking producer; its return value becomes the ``done`` payload.
        allowed_events: Event names this transport accepts; anything else is a
            protocol violation and terminates the stream with an ``error`` frame.
        failure: Maps any exception, including a protocol violation, to the public
            ``error`` payload. Callers own this mapping so no upstream text leaks.
        frame: The wire encoder, injected so an application can keep its own
            (and be replaced in tests) rather than have two encoders diverge.

    Returns:
        A ``text/event-stream`` response. ``done`` is sent only after ``execute``
        returns, which is what lets a caller persist before acknowledging.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    finished = object()
    terminal_lock = threading.Lock()
    terminal_sent = False

    def enqueue(event: dict, *, terminal: bool = False) -> None:
        nonlocal terminal_sent
        with terminal_lock:
            if terminal_sent:
                return
            terminal_sent = terminal
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def emit(event: dict) -> None:
        snapshot = event_snapshot(event, allowed_events)
        if snapshot is None:
            enqueue({"event": "error", "data": failure(ValueError("Invalid stream event"))},
                    terminal=True)
            return
        enqueue(snapshot)

    def work() -> None:
        try:
            enqueue({"event": "done", "data": execute(emit)}, terminal=True)
        except Exception as error:  # noqa: BLE001 - never expose raw provider responses
            enqueue({"event": "error", "data": failure(error)}, terminal=True)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, finished)

    async def events() -> AsyncIterator[str]:
        loop.run_in_executor(None, work)
        while True:
            item = await queue.get()
            if item is finished:
                return
            yield frame(item)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

"""Sync pub/sub event bus for the bridge.

CrewAI's bus does sync + async + RWLock + dependency graph (see [[CREWAI-
EVALUATION]] §2.3). ANTON is single-user; we copy the **API shape** but
drop everything past sync handlers in a ``ThreadPoolExecutor``.

Contract:
  * ``bridge_event_bus.on(EventType)(handler)`` — register a handler. Match
    against an event class; subclass events also trigger handlers registered
    on the base class. Registration is IDEMPOTENT: re-registering the same
    callable for the same event type is a no-op, so lifespan re-entry under
    FastAPI reload cannot stack duplicate handlers.
  * ``bridge_event_bus.emit(event)`` → ``Future``. The future resolves once
    every handler has run (or errored). Handler exceptions are logged but
    NEVER propagate — observability MUST NOT break the calling workflow.
  * ``bridge_event_bus.wait()`` — drain pending dispatches (test helper).

Singleton: import ``bridge_event_bus`` from this module; tests reset it
via ``BridgeEventBus`` constructor."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TypeVar

from routines.hooks.events import Event

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=Event)


class BridgeEventBus:
    """Tiny sync pub/sub. Thread-safe register / emit."""

    def __init__(self, max_workers: int = 4) -> None:
        self._handlers: dict[type[Event], list[Callable[[Event], Any]]] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="bridge-event-bus",
        )

    def on(self, event_type: type[E]) -> Callable[[Callable[[E], Any]], Callable[[E], Any]]:
        """Register a handler for ``event_type`` (and its subclasses).

        Idempotent per ``(event_type, handler)`` pair — registering the same
        callable OBJECT again is a no-op (``is`` identity check under the
        lock, mirroring app.py's ``_ensure()`` hook re-attach; equality is
        deliberately not consulted, so equal-but-distinct callables — fresh
        closures, per-call bound methods, eq-overriding instances — all
        register, and no foreign ``__eq__`` runs under the lock). Callers
        wanting reload-safe dedup must register a stable reference, as
        ``register_central_guards()`` does: it runs on every lifespan start,
        and without the check each FastAPI reload stacked another copy of the
        six audit-writing ``_on_skill_*`` subscribers → duplicate audit rows
        per skill lifecycle event.

        Usage::

            @bridge_event_bus.on(SkillInvocationStarted)
            def log_started(evt):
                logger.info("skill %s started", evt.skill)
        """

        def decorator(func: Callable[[E], Any]) -> Callable[[E], Any]:
            with self._lock:
                handlers = self._handlers.setdefault(event_type, [])
                if not any(registered is func for registered in handlers):
                    handlers.append(func)  # type: ignore[arg-type]
            return func

        return decorator

    def emit(self, event: Event) -> Future:
        """Fire ``event`` to every matching handler. Returns a future that
        resolves once all handlers have run."""
        with self._lock:
            matched: list[Callable[[Event], Any]] = []
            for registered_type, handlers in self._handlers.items():
                if isinstance(event, registered_type):
                    matched.extend(handlers)

        def _dispatch() -> None:
            for handler in matched:
                try:
                    handler(event)
                except Exception as e:  # noqa: BLE001 — observability must not break the workflow
                    logger.warning(
                        "event handler %s failed on %s: %s",
                        getattr(handler, "__name__", "?"),
                        type(event).__name__,
                        e,
                    )

        return self._executor.submit(_dispatch)

    def clear(self) -> None:
        """Test helper — drop every registered handler."""
        with self._lock:
            self._handlers.clear()

    def shutdown(self, wait: bool = True) -> None:
        """Stop the executor — call at app shutdown."""
        self._executor.shutdown(wait=wait)


bridge_event_bus = BridgeEventBus()

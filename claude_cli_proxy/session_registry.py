"""Session state for MCP tool bridge.

Two-tier session model:
  ContextCache   — long-lived, survives across requests.  Stores the pre-fetched
                   environment snapshot (file tree, git state, CLAUDE.md …).
                   Keyed by x-session-id.  Never automatically expired — the
                   client invalidates by sending x-refresh-context: true.

  SessionState   — per-request, alive only for the duration of one SSE stream.
                   Holds the asyncio plumbing for the MCP tool-call bridge
                   (event_queue, pending calls).  Caller must unregister() in
                   a finally block.
"""

import asyncio
from dataclasses import dataclass, field


class SessionNotFoundError(KeyError):
    pass


class ContextCache:
    """Long-lived store of pre-fetched environment snapshots, one per session_id."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, session_id: str) -> str | None:
        return self._store.get(session_id)

    def set(self, session_id: str, snapshot: str) -> None:
        self._store[session_id] = snapshot

    def invalidate(self, session_id: str) -> None:
        self._store.pop(session_id, None)


@dataclass
class PendingToolCall:
    """A tool call dispatched to the phone and awaiting its result."""
    id: str
    name: str
    tool_input: dict
    result_event: asyncio.Event = field(default_factory=asyncio.Event)
    result: str | None = None
    error: str | None = None


@dataclass
class SessionState:
    session_id: str
    tools: list[dict]
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    _pending: dict[str, PendingToolCall] = field(default_factory=dict)


class SessionRegistry:
    """Thread-safe (asyncio-safe) registry of active sandbox sessions."""

    def __init__(self):
        self._sessions: dict[str, SessionState] = {}
        self._call_index: dict[str, str] = {}

    def register(self, session_id: str, tools: list[dict]) -> SessionState:
        state = SessionState(session_id=session_id, tools=tools)
        self._sessions[session_id] = state
        return state

    def get(self, session_id: str) -> SessionState:
        try:
            return self._sessions[session_id]
        except KeyError:
            raise SessionNotFoundError(session_id)

    def unregister(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session:
            for call_id, call in list(session._pending.items()):
                if not call.result_event.is_set():
                    call.error = "session terminated"
                    call.result_event.set()
                self._call_index.pop(call_id, None)

    def add_pending_call(
        self, session_id: str, call_id: str, name: str, tool_input: dict
    ) -> PendingToolCall:
        session = self.get(session_id)
        if call_id in session._pending:
            raise ValueError(f"Duplicate call_id: {call_id}")
        call = PendingToolCall(id=call_id, name=name, tool_input=tool_input)
        session._pending[call_id] = call
        self._call_index[call_id] = session_id
        return call

    def resolve_call(self, call_id: str, result: str) -> None:
        session_id = self._call_index.get(call_id)
        if not session_id:
            return
        session = self._sessions.get(session_id)
        if not session:
            return
        call = session._pending.get(call_id)
        if call:
            call.result = result
            call.result_event.set()

    def reject_call(self, call_id: str, error: str) -> None:
        session_id = self._call_index.get(call_id)
        if not session_id:
            return
        session = self._sessions.get(session_id)
        if not session:
            return
        call = session._pending.get(call_id)
        if call:
            call.error = error
            call.result_event.set()

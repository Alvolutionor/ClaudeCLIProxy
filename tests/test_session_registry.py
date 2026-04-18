import pytest
from claude_cli_proxy.session_registry import SessionRegistry, SessionNotFoundError


def test_register_and_get():
    reg = SessionRegistry()
    reg.register("s1", [{"function": {"name": "read_file"}}])
    session = reg.get("s1")
    assert session.session_id == "s1"
    assert len(session.tools) == 1


def test_get_unknown_raises():
    reg = SessionRegistry()
    with pytest.raises(SessionNotFoundError):
        reg.get("does-not-exist")


def test_unregister_cleans_up():
    reg = SessionRegistry()
    reg.register("s1", [])
    reg.unregister("s1")
    with pytest.raises(SessionNotFoundError):
        reg.get("s1")


async def test_add_and_resolve_pending_call():
    reg = SessionRegistry()
    reg.register("s1", [])
    call = reg.add_pending_call("s1", "tc1", "read_file", {"path": "foo.ts"})
    assert call.id == "tc1"
    assert not call.result_event.is_set()

    reg.resolve_call("tc1", "file contents")
    assert call.result_event.is_set()
    assert call.result == "file contents"


async def test_reject_pending_call():
    reg = SessionRegistry()
    reg.register("s1", [])
    call = reg.add_pending_call("s1", "tc2", "edit_file", {})
    reg.reject_call("tc2", "permission denied")
    assert call.result_event.is_set()
    assert call.error == "permission denied"


def test_resolve_unknown_call_is_noop():
    reg = SessionRegistry()
    reg.resolve_call("nonexistent-call-id", "result")


def test_unregister_signals_pending_and_cleans_call_index():
    reg = SessionRegistry()
    reg.register("s1", [])
    call = reg.add_pending_call("s1", "tc1", "fn", {})
    reg.unregister("s1")
    # Pending waiter should be signaled with "session terminated"
    assert call.result_event.is_set()
    assert call.error == "session terminated"
    # _call_index entry should be gone: late resolve must be a no-op, not raise
    reg.resolve_call("tc1", "late result")

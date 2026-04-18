# tests/test_conversation_store.py
from claude_cli_proxy.conversation_store import ConversationStore


def test_set_and_get():
    store = ConversationStore()
    store.set("client-1", "claude-abc")
    assert store.get("client-1") == "claude-abc"


def test_get_missing_returns_none():
    store = ConversationStore()
    assert store.get("nonexistent") is None


def test_delete():
    store = ConversationStore()
    store.set("client-1", "claude-abc")
    store.delete("client-1")
    assert store.get("client-1") is None


def test_overwrite():
    store = ConversationStore()
    store.set("client-1", "claude-abc")
    store.set("client-1", "claude-xyz")
    assert store.get("client-1") == "claude-xyz"

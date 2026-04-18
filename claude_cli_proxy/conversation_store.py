"""对话 session 持久化存储。

映射 client 发来的 x-session-id → Claude CLI 返回的 session_id，
使后续请求可以用 --resume 续接，无需传全量 history。
"""


class ConversationStore:
    """内存映射：client_session_id → claude_session_id。"""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, client_id: str) -> str | None:
        return self._store.get(client_id)

    def set(self, client_id: str, claude_session_id: str) -> None:
        self._store[client_id] = claude_session_id

    def delete(self, client_id: str) -> None:
        self._store.pop(client_id, None)

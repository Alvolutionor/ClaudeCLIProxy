"""OpenAI 协议转换工具模块。

负责 OpenAI 格式的请求/响应与 Claude CLI 之间的转换，
包括标准响应和 SSE 流式响应。
"""

import json
import time
import uuid

# 通过 API 暴露的可用模型列表
AVAILABLE_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
]


def _extract_text(content) -> str:
    """从消息内容中提取纯文本（仅用于 --resume 模式的最后一条用户消息）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(parts)
    return str(content) if content else ""


def render_content(content) -> str:
    """将消息内容渲染为结构化文本，保留 tool_use / tool_result 语义。

    支持三种 block 类型:
    - text       → 原文输出
    - tool_use   → [Tool call: name(input_json)]
    - tool_result→ [Tool result for <id>]: content
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content else ""

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
        elif btype == "tool_use":
            name = block.get("name", "")
            inp = block.get("input", {})
            parts.append(f"[Tool call: {name}({json.dumps(inp, ensure_ascii=False)})]")
        elif btype == "tool_result":
            tool_id = block.get("tool_use_id", "")
            result = block.get("content", "")
            if isinstance(result, list):
                result = "\n".join(
                    b.get("text", "") for b in result
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            parts.append(f"[Tool result for {tool_id}]: {result}")
    return "\n".join(parts)


# 注入到每个请求最前面的隔离指令，确保 Claude CLI 只使用 prompt 中的显式上下文，
# 而不会读取 proxy 运行目录、git 仓库或 CLAUDE.md 等本地环境信息。
_CONTEXT_GUARDRAIL = (
    "Ignore any implicit local filesystem, git, working-directory, "
    "or CLAUDE.md context from your runtime environment. "
    "Use only the explicit context provided in this prompt and the conversation."
)


def messages_to_prompt(messages: list[dict]) -> str:
    """将 OpenAI 格式的消息列表转换为单个提示字符串。

    参数:
        messages: 字典列表，格式为 {"role": ..., "content": ...}。

    返回:
        拼接后的提示字符串，供 Claude CLI 使用。
        开头始终注入上下文隔离指令，确保 Claude 只读取 prompt 中的显式内容。
    """
    parts: list[str] = [_CONTEXT_GUARDRAIL]
    for msg in messages:
        role = msg.get("role", "user")
        content = render_content(msg.get("content", ""))
        if not content:
            continue
        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            parts.append(f"[Previous assistant response]: {content}")
        else:
            parts.append(content)
    return "\n".join(parts)


def last_user_prompt(messages: list[dict]) -> str:
    """从消息列表中提取最后一条 user 消息的文本。

    用于 --resume 模式：session 已有历史，只需传最新的用户输入。
    如果没有 user 消息，返回空字符串。
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            return _extract_text(msg.get("content", ""))
    return ""


def build_chat_response(model: str, content: str, prompt_chars: int = 0) -> dict:
    """构建 OpenAI 兼容的聊天补全响应。

    参数:
        model: 使用的模型标识符。
        content: 助手的响应文本。
        prompt_chars: 输入提示的字符数，用于估算 prompt_tokens。

    返回:
        符合 OpenAI ChatCompletion 格式的字典。
    """
    # 粗略的 token 估算（约 3 字符/token，在英文和中日韩文之间折中）
    prompt_tokens = max(prompt_chars // 3, 1) if prompt_chars else len(content) // 4
    completion_tokens = max(len(content) // 3, 1)

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def build_stream_chunks(model: str, content: str, chunk_size: int = 20) -> list[str]:
    """将响应内容拆分为 SSE 流式数据块。

    模拟 OpenAI 的流式响应格式，将完整内容拆分为多个块进行传输。

    参数:
        model: 使用的模型标识符。
        content: 助手的完整响应文本。
        chunk_size: 每个数据块的字符数（默认: 20）。

    返回:
        SSE 格式的字符串列表，每条以 "data: " 为前缀，
        最后一条为 "data: [DONE]"。
    """
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    chunks: list[str] = []

    # 第一个块：角色信息（内容为空）
    first_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    }
    chunks.append(f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n")

    # 按 chunk_size 拆分内容块
    for i in range(0, len(content), chunk_size):
        piece = content[i : i + chunk_size]
        chunk = {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        chunks.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")

    # 最终块：停止信号
    stop_chunk = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    chunks.append(f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n")
    chunks.append("data: [DONE]\n\n")

    return chunks


def build_models_response() -> dict:
    """构建 OpenAI 兼容的模型列表响应。"""
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "anthropic"}
            for m in AVAILABLE_MODELS
        ],
    }

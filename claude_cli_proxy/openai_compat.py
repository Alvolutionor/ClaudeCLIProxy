"""OpenAI protocol conversion utilities."""

import time
import uuid

# Available models exposed through the API
AVAILABLE_MODELS = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
]


def messages_to_prompt(messages: list[dict]) -> str:
    """Convert OpenAI-style messages list to a single prompt string.

    Args:
        messages: List of {"role": ..., "content": ...} dicts.

    Returns:
        A single prompt string for the Claude CLI.
    """
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            parts.append(f"[System instruction]: {content}")
        elif role == "assistant":
            parts.append(f"[Previous assistant response]: {content}")
        else:
            parts.append(content)
    return "\n".join(parts)


def build_chat_response(model: str, content: str) -> dict:
    """Build an OpenAI-compatible chat completion response.

    Args:
        model: The model identifier used.
        content: The assistant's response text.

    Returns:
        Dict matching the OpenAI ChatCompletion schema.
    """
    prompt_tokens = len(content) // 4  # rough estimate
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
            "completion_tokens": len(content) // 4,
            "total_tokens": prompt_tokens + len(content) // 4,
        },
    }


def build_models_response() -> dict:
    """Build an OpenAI-compatible model list response."""
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "owned_by": "anthropic"}
            for m in AVAILABLE_MODELS
        ],
    }

"""Context pre-fetch and system prompt assembly.

Pre-fetch phase: before starting Claude CLI, the proxy proactively calls
standard 'environment' tools on the phone to build a rich context snapshot —
mirroring what Claude Code collects locally at session start (cwd, git status,
file tree, project instructions, open file, diagnostics).

Results are assembled into a structured system prefix and cached per
long-lived session_id. Subsequent requests in the same session skip pre-fetch
and reuse the snapshot.

Two-tier context:
  Session-level (pre-fetched once, cached):
    - project file structure, CLAUDE.md, README, git state, project configs
  Request-level (fresh every request, from client body):
    - currently open file + content + cursor, editor diagnostics
"""

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger("claude_cli_proxy.context_builder")

# Files read via read_file during pre-fetch, in priority order.
# Each path is optional — failures are silently skipped.
STANDARD_READ_PATHS = [
    "CLAUDE.md",
    "README.md",
    "package.json",
    "pyproject.toml",
]

# Tool names the proxy recognises as environment tools.
# If a client registers a tool with one of these names, it participates
# in pre-fetch automatically.
_ENV_TOOLS = {"list_directory", "read_file", "git_status", "git_log"}


@dataclass
class PrefetchCall:
    tool_name: str
    arguments: dict
    label: str
    optional: bool = True


def _standard_calls(registered: set[str], extra: list[dict]) -> list[PrefetchCall]:
    """Build the ordered pre-fetch call list from registered tools + client extras."""
    calls: list[PrefetchCall] = []

    if "list_directory" in registered:
        calls.append(PrefetchCall("list_directory", {"path": "."}, "Project structure", optional=False))

    if "read_file" in registered:
        for path in STANDARD_READ_PATHS:
            calls.append(PrefetchCall("read_file", {"path": path}, f"File: {path}"))

    if "git_status" in registered:
        calls.append(PrefetchCall("git_status", {}, "Git status"))

    if "git_log" in registered:
        calls.append(PrefetchCall("git_log", {"limit": 10}, "Recent commits"))

    for e in extra:
        name = e.get("tool", "")
        if name and name in registered:
            calls.append(PrefetchCall(
                tool_name=name,
                arguments=e.get("arguments", {}),
                label=e.get("label", name),
            ))

    return calls


async def run_prefetch(
    session_id: str,
    registry,
    tools: list[dict],
    extra_prefetch: list[dict],
    tool_timeout: float,
    write_sse,
) -> dict[str, str]:
    """Drive pre-fetch tool calls over the open SSE channel.

    For each call: register a PendingToolCall, push a tool_call SSE event to
    the phone, then block until the phone POSTs the result back.  The phone
    sees these identically to Claude-initiated tool calls and handles them with
    the same code path.

    Returns a dict of {label: result_text} for every successful call.
    """
    registered = {t.get("function", {}).get("name", "") for t in tools}
    calls = _standard_calls(registered, extra_prefetch)
    results: dict[str, str] = {}

    for call in calls:
        call_id = f"pf_{secrets.token_hex(8)}"
        pending = registry.add_pending_call(session_id, call_id, call.tool_name, call.arguments)

        event = json.dumps({
            "type": "tool_call",
            "id": call_id,
            "name": call.tool_name,
            "input": call.arguments,
        })
        await write_sse(f"data: {event}\n\n")

        try:
            await asyncio.wait_for(pending.result_event.wait(), timeout=tool_timeout)
        except asyncio.TimeoutError:
            logger.warning("[prefetch] %s timed out after %.0fs", call.tool_name, tool_timeout)
            if not call.optional:
                results[call.label] = "[timed out]"
            continue

        if pending.error is not None:
            logger.debug("[prefetch] %s skipped: %s", call.tool_name, pending.error)
            if not call.optional:
                results[call.label] = f"[error: {pending.error}]"
        elif pending.result:
            results[call.label] = pending.result

    return results


def assemble_env_snapshot(results: dict[str, str], meta: dict) -> str:
    """Assemble pre-fetch results into a structured environment context block."""
    lines: list[str] = [
        "## Environment",
        f"Working directory: {meta.get('cwd', 'unknown')}",
        f"Platform: {meta.get('platform', 'unknown')}",
        f"Date: {time.strftime('%Y-%m-%d')}",
    ]

    for label, content in results.items():
        lines.append(f"\n### {label}")
        lines.append(content.strip())

    return "\n".join(lines)


def build_cli_prompt(
    messages: list[dict],
    env_snapshot: str | None,
    open_file: dict | None,
    diagnostics: list[dict],
) -> str:
    """Build the full CLI prompt with environment context prepended to messages.

    Sections (all optional except messages):
      1. Guardrail — tells CLI to use only the explicit context provided
      2. Environment snapshot — session-level pre-fetched context
      3. Open file — currently active file in the editor (request-level)
      4. Diagnostics — editor lint/type errors (request-level)
      5. Conversation messages
    """
    from .openai_compat import render_content

    _GUARDRAIL = (
        "You are a coding assistant operating in a remote mobile development environment. "
        "Ignore any implicit local filesystem, git, or CLAUDE.md context from your runtime. "
        "Use only the explicit context provided below."
    )

    parts: list[str] = [_GUARDRAIL]

    if env_snapshot:
        parts.append(env_snapshot)

    if open_file:
        path = open_file.get("path", "")
        content = open_file.get("content", "")
        cursor = open_file.get("cursor_line")
        selection = open_file.get("selection", "")

        file_lines = [f"## Currently open file: {path}"]
        if cursor:
            file_lines.append(f"Cursor at line {cursor}")
        if selection:
            file_lines.append(f"Selected:\n```\n{selection}\n```")
        if content:
            file_lines.append(f"Content:\n```\n{content}\n```")
        parts.append("\n".join(file_lines))

    if diagnostics:
        diag_lines = ["## Editor diagnostics"]
        for d in diagnostics:
            sev = d.get("severity", "info")
            line_no = d.get("line", "?")
            msg = d.get("message", "")
            diag_lines.append(f"  Line {line_no} [{sev}]: {msg}")
        parts.append("\n".join(diag_lines))

    for msg in messages:
        role = msg.get("role", "user")
        text = render_content(msg.get("content", ""))
        if not text:
            continue
        if role == "system":
            parts.append(f"[System]: {text}")
        elif role == "assistant":
            parts.append(f"[Assistant]: {text}")
        else:
            parts.append(text)

    return "\n\n".join(parts)

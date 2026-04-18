"""Integration tests for MCP JSON-RPC handlers.

We instantiate MCPServer directly and call its internal _process_message()
to test each JSON-RPC method without needing a running HTTP server.
"""

import asyncio
import pytest
from claude_cli_proxy.session_registry import SessionRegistry
from claude_cli_proxy.mcp_server import MCPServer

SESSION_ID = "test-session"


@pytest.fixture
def registry_with_tools():
    reg = SessionRegistry()
    reg.register(SESSION_ID, [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                }
            }
        }
    ])
    return reg


async def test_initialize_returns_capabilities(registry_with_tools):
    server = MCPServer(registry_with_tools, mcp_port=19999)
    queue = asyncio.Queue()
    server._response_queues[SESSION_ID] = queue

    await server._process_message(SESSION_ID, {
        "jsonrpc": "2.0", "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}}
    })

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["id"] == 1
    assert msg["result"]["capabilities"]["tools"] == {}
    assert msg["result"]["serverInfo"]["name"] == "mobile-sandbox"


async def test_tools_list_returns_registered_tools(registry_with_tools):
    server = MCPServer(registry_with_tools, mcp_port=19999)
    queue = asyncio.Queue()
    server._response_queues[SESSION_ID] = queue

    await server._process_message(SESSION_ID, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
    })

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    tools = msg["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "read_file"
    assert "path" in tools[0]["inputSchema"]["properties"]


async def test_tools_call_enqueues_event_and_resolves(registry_with_tools):
    server = MCPServer(registry_with_tools, mcp_port=19999)
    queue = asyncio.Queue()
    server._response_queues[SESSION_ID] = queue

    session = registry_with_tools.get(SESSION_ID)

    # Start tools/call in background (it will block until result)
    task = asyncio.ensure_future(server._process_message(SESSION_ID, {
        "jsonrpc": "2.0", "id": 3,
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "src/app.tsx"}}
    }))

    # The call should appear in the session's event_queue
    call = await asyncio.wait_for(session.event_queue.get(), timeout=1.0)
    assert call.name == "read_file"
    assert call.tool_input == {"path": "src/app.tsx"}   # NOTE: tool_input not input

    # Simulate phone returning result
    registry_with_tools.resolve_call(call.id, "import React from 'react';")

    await asyncio.wait_for(task, timeout=1.0)

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["id"] == 3
    assert msg["result"]["content"][0]["text"] == "import React from 'react';"


async def test_tools_call_timeout(registry_with_tools):
    server = MCPServer(registry_with_tools, mcp_port=19999, tool_timeout=0.1)
    queue = asyncio.Queue()
    server._response_queues[SESSION_ID] = queue

    await server._process_message(SESSION_ID, {
        "jsonrpc": "2.0", "id": 4,
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "x"}}
    })

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["id"] == 4
    assert msg["result"]["isError"] is True
    assert "timed out" in msg["result"]["content"][0]["text"]


async def test_tools_call_returns_error_on_reject(registry_with_tools):
    server = MCPServer(registry_with_tools, mcp_port=19999)
    queue = asyncio.Queue()
    server._response_queues[SESSION_ID] = queue

    session = registry_with_tools.get(SESSION_ID)

    task = asyncio.ensure_future(server._process_message(SESSION_ID, {
        "jsonrpc": "2.0", "id": 5,
        "method": "tools/call",
        "params": {"name": "read_file", "arguments": {"path": "secret.ts"}}
    }))

    call = await asyncio.wait_for(session.event_queue.get(), timeout=1.0)
    # Simulate phone reporting execution error
    registry_with_tools.reject_call(call.id, "permission denied")

    await asyncio.wait_for(task, timeout=1.0)

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["id"] == 5
    assert msg["result"]["isError"] is True
    assert "permission denied" in msg["result"]["content"][0]["text"]

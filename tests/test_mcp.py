# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tools of a local MCP server (``MCPToolkit``) run through agent middleware, so the guard checks their calls."""

from __future__ import annotations

import sys
from pathlib import Path

from ag2.tools.toolkits.mcp_server import MCPStdioServerConfig, MCPToolkit

from ag2_acgs import ACGSGuard
from tests.fakes import HELLO, LEAK, agent, call, model, results, text

SERVER = Path(__file__).with_name("mcp_server.py")


async def test_mcp_tool_calls_are_checked(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    toolkit = MCPToolkit(MCPStdioServerConfig(command=sys.executable, args=[str(SERVER), str(log)]))
    config = model([call("send_email", LEAK, "m1"), call("send_email", HELLO, "m2")], "Done.")
    reply = await agent(config, [toolkit], [ACGSGuard().middleware]).ask("Email them")
    found = await results(reply)
    assert text(found["m1"]).startswith("Tool call 'send_email' was not executed. Blocked by ACGS rule ACGS-006")
    assert "sent to friend@example.com" in text(found["m2"])
    assert log.read_text(encoding="utf-8").splitlines() == ["send_email friend@example.com"]

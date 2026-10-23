# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Tools whose calls never reach agent middleware are refused, so they cannot bypass ACGS."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest
from ag2 import Context, MemoryStream, Middleware, TaskConfig, Toolkit
from ag2.events import BuiltinToolCallEvent, ModelRequest, ModelResponse
from ag2.middleware import BaseMiddleware
from ag2.testing import TrackingConfig
from ag2.tools.builtin import (
    AnthropicBashTool,
    CodeExecutionTool,
    FileSearchTool,
    ImageGenerationTool,
    MCPServerTool,
    ToolSearchTool,
    WebFetchTool,
    WebSearchTool,
)

from ag2_acgs import ACGSGuard, ACGSMiddleware, UngovernedToolsError
from tests.fakes import HELLO, LEAK, Tools, agent, call, model, results, text

HOSTED = [
    WebSearchTool(),
    WebFetchTool(),
    CodeExecutionTool(),
    ImageGenerationTool(),
    FileSearchTool(vector_store_ids=["vs_1"]),
    MCPServerTool(server_url="https://mcp.example.com", server_label="example"),
]


@pytest.mark.parametrize("hosted", HOSTED, ids=lambda tool: type(tool).__name__)
async def test_hosted_tools_are_refused_before_the_model_is_called(hosted: Any) -> None:
    tools = Tools()
    config = TrackingConfig(model(call("send_email", HELLO, "e1"), "Done."))
    governed = agent(config, [tools.send_email, hosted], [ACGSGuard().middleware])  # type: ignore[arg-type]
    with pytest.raises(UngovernedToolsError, match=f"provider-hosted tool .*{type(hosted).__name__}"):
        await governed.ask("x")
    assert config.calls == []
    assert tools.ran == []


async def test_hosted_tool_inside_a_toolkit_is_refused() -> None:
    tools = Tools()
    governed = agent(model("Done."), [Toolkit(tools.send_email, WebSearchTool())], [ACGSGuard().middleware])
    with pytest.raises(UngovernedToolsError, match="WebSearchTool"):
        await governed.ask("x")


def test_local_tools_are_governed() -> None:
    tools = Tools()
    governed = agent(
        model(),
        [tools.send_email, Toolkit(tools.issue_refund), AnthropicBashTool(), ToolSearchTool(tools.send_email)],
    )
    assert ACGSGuard.ungoverned_tools(governed) == []


def test_a_tool_listed_twice_is_reported_once() -> None:
    search = WebSearchTool()
    fake_agent = SimpleNamespace(tools=(search, search), tasks=None)
    assert ACGSGuard.ungoverned_tools(fake_agent) == ["provider-hosted tool 'web_search' (WebSearchTool)"]


async def test_sub_tasks_are_refused() -> None:
    tools = Tools()
    governed = agent(model("Done."), [tools.send_email], [ACGSGuard().middleware], tasks=TaskConfig())
    with pytest.raises(UngovernedToolsError, match=r"sub-tasks \(tasks=TaskConfig"):
        await governed.ask("x")


def test_sub_agents_need_their_own_guard() -> None:
    tools = Tools()
    guard = ACGSGuard()

    class Other(BaseMiddleware):
        pass

    worker = agent(model(), [tools.send_email], [Middleware(Other)])
    governed_worker = agent(model(), [tools.send_email], [Middleware(Other), guard.middleware])
    no_tools = agent(model(), [])
    parent = agent(
        model(),
        [
            worker.as_tool(description="Worker.", name="worker"),
            governed_worker.as_tool(description="Governed worker.", name="governed"),
            no_tools.as_tool(description="Talker.", name="talker"),
        ],
    )
    assert guard.ungoverned_tools(parent) == [
        "sub-agent tool 'worker' (agent 'assistant' runs its tools without ACGS middleware)"
    ]


async def test_sub_agent_without_a_guard_is_refused_at_run_time() -> None:
    tools = Tools()
    worker = agent(model(), [tools.send_email])
    parent = agent(model("Done."), [worker.as_tool(description="Worker.")], [ACGSGuard().middleware])
    with pytest.raises(UngovernedToolsError, match="sub-agent tool"):
        await parent.ask("x")


async def test_allow_ungoverned_logs_a_warning_and_still_governs_local_tools(caplog: pytest.LogCaptureFixture) -> None:
    tools = Tools()
    config = model([call("send_email", LEAK, "e1"), call("send_email", HELLO, "e2")], "Done.")
    governed = agent(config, [tools.send_email, WebSearchTool()], [ACGSGuard(allow_ungoverned=True).middleware])
    with caplog.at_level(logging.WARNING, logger="ag2_acgs.middleware"):
        reply = await governed.ask("x")
    assert "ACGS does not check calls of: provider-hosted tool 'web_search'" in caplog.text
    assert tools.ran == [("send_email", HELLO)]
    assert text((await results(reply))["e1"]).startswith("Tool call 'send_email' was not executed.")


async def test_hosted_tool_added_for_one_ask_ends_the_turn_after_the_provider_ran_it() -> None:
    tools = Tools()
    config = model(BuiltinToolCallEvent("web_search", arguments='{"query": "x"}'), call("send_email", HELLO, "e1"))
    governed = agent(config, [tools.send_email], [ACGSGuard().middleware])
    with pytest.raises(UngovernedToolsError, match=r"provider-hosted tool call 'web_search' \(already run"):
        await governed.ask("x", tools=[WebSearchTool()])
    assert tools.ran == []


async def test_hosted_tool_calls_are_logged_with_allow_ungoverned(caplog: pytest.LogCaptureFixture) -> None:
    config = model(BuiltinToolCallEvent("web_search", arguments="{}"), "Done.")
    governed = agent(config, [], [ACGSGuard(allow_ungoverned=True).middleware])
    with caplog.at_level(logging.WARNING, logger="ag2_acgs.middleware"):
        reply = await governed.ask("x", tools=[WebSearchTool()])
    assert reply.body == "Done."
    assert "provider-hosted tool call 'web_search'" in caplog.text


async def test_turn_without_an_agent_skips_the_agent_check() -> None:
    context = Context(MemoryStream())
    event = ModelRequest.ensure_request(["x"])
    middleware = ACGSMiddleware(event, context, guard=ACGSGuard())
    expected = ModelResponse()

    async def turn(event: Any, context: Context) -> ModelResponse:
        return expected

    assert await middleware.on_turn(turn, event, context) is expected


async def test_a_hosted_call_named_like_a_local_tool_still_goes_through_the_guard() -> None:
    tools = Tools()
    config = model(
        BuiltinToolCallEvent("send_email", arguments='{"to": "a@example.com", "body": "The password is hunter2"}'),
        "Done.",
    )
    governed = agent(config, [tools.send_email], [ACGSGuard().middleware])
    with pytest.raises(UngovernedToolsError, match="provider-hosted tool call 'send_email'"):
        await governed.ask("x")
    assert tools.ran == []


async def test_a_call_to_an_unknown_tool_runs_nothing() -> None:
    tools = Tools()
    config = model(call("delete_everything", {}, "d1"), "Done.")
    reply = await agent(config, [tools.send_email], [ACGSGuard().middleware]).ask("x")
    assert tools.ran == []
    assert reply.body == "Done."

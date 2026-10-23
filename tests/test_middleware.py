# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Blocking and allowing calls through the real AG2 agent loop."""

from __future__ import annotations

from typing import Any

import pytest
from ag2 import Middleware, Toolkit
from ag2.events import ToolCallEvent
from ag2.middleware import BaseMiddleware
from ag2.middleware.base import ToolExecution, ToolResultType
from ag2.tools.final.client_tool import ClientTool

from ag2_acgs import DECISION_METADATA_KEY, ACGSGuard, ACGSMiddleware, GovernanceDecision, Verdict
from tests.fakes import BLOCKED, HELLO, LEAK, Tools, agent, call, model, results, text


def test_options_are_validated() -> None:
    with pytest.raises(ValueError, match="on_review"):
        ACGSGuard(on_review="approve")  # type: ignore[arg-type]
    for timeout in (0, -1.0):
        with pytest.raises(ValueError, match="review_timeout"):
            ACGSGuard(review_timeout=timeout)


def test_repr_shows_options() -> None:
    assert repr(ACGSGuard(review_timeout=30)) == (
        "ACGSGuard(agent_id='ag2', on_review='ask', review_timeout=30, allow_ungoverned=False)"
    )


def test_middleware_is_an_ag2_middleware_factory() -> None:
    guard = ACGSGuard()
    assert isinstance(guard.middleware, Middleware)
    assert guard.middleware.cls is ACGSMiddleware
    assert guard.middleware.options["guard"] is guard


def test_evaluate_uses_the_default_constitution() -> None:
    guard = ACGSGuard()
    assert guard.evaluate("send_email", LEAK).verdict is Verdict.DENY
    assert guard.evaluate("send_email", HELLO).verdict is Verdict.ALLOW


async def test_blocked_call_is_not_executed_and_the_model_reads_the_reason() -> None:
    tools = Tools()
    config = model([call("send_email", LEAK, "e1"), call("send_email", HELLO, "e2")], "Done.")
    reply = await agent(config, [tools.send_email], [ACGSGuard().middleware]).ask("Email them")
    assert reply.body == "Done."
    assert tools.ran == [("send_email", HELLO)]
    by_id = await results(reply)
    assert text(by_id["e1"]).startswith(BLOCKED)
    assert text(by_id["e2"]) == "sent to friend@example.com"
    decision = by_id["e1"].result.metadata[DECISION_METADATA_KEY]
    assert decision["verdict"] == "deny"
    assert decision["rule_ids"] == ["ACGS-006"]
    assert decision["error"] is None
    assert DECISION_METADATA_KEY not in by_id["e2"].result.metadata


async def test_the_reason_never_repeats_argument_values() -> None:
    tools = Tools()
    reply = await agent(
        model(call("send_email", LEAK, "e1"), "Done."), [tools.send_email], [ACGSGuard().middleware]
    ).ask("x")
    assert "hunter2" not in text((await results(reply))["e1"])


async def test_guard_can_be_added_for_one_ask() -> None:
    tools = Tools()
    governed = agent(model(call("send_email", LEAK, "e1"), "Done."), [tools.send_email])
    reply = await governed.ask("x", middleware=[ACGSGuard().middleware])
    assert tools.ran == []
    assert text((await results(reply))["e1"]).startswith(BLOCKED)


@pytest.mark.parametrize("arguments", ["not json", "[1, 2", '{"to": '])
async def test_malformed_arguments_are_denied(arguments: str) -> None:
    tools = Tools()
    config = model(ToolCallEvent("send_email", arguments=arguments, id="e1"), "Done.")
    reply = await agent(config, [tools.send_email], [ACGSGuard().middleware]).ask("x")
    assert tools.ran == []
    result = (await results(reply))["e1"]
    assert "could not evaluate" in text(result)
    assert result.result.metadata[DECISION_METADATA_KEY]["error"].startswith("JSONDecodeError")


async def test_an_error_in_the_guard_denies_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = ACGSGuard()

    def broken(tool_name: str, arguments: Any) -> GovernanceDecision:
        raise RuntimeError("engine down")

    monkeypatch.setattr(guard, "evaluate", broken)
    tools = Tools()
    reply = await agent(model(call("send_email", HELLO, "e1"), "Done."), [tools.send_email], [guard.middleware]).ask(
        "x"
    )
    assert tools.ran == []
    result = (await results(reply))["e1"]
    assert text(result) == (
        "Tool call 'send_email' was not executed. ACGS governance could not evaluate this tool call, so it was denied."
    )
    assert result.result.metadata[DECISION_METADATA_KEY]["error"] == "RuntimeError: engine down"


async def test_toolkit_and_client_tools_are_governed() -> None:
    tools = Tools()
    client = ClientTool(
        {
            "type": "function",
            "function": {
                "name": "post_message",
                "description": "Post a message.",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
        }
    )
    config = model(
        [
            call("send_email", LEAK, "e1"),
            call("post_message", {"text": "The password is hunter2"}, "p1"),
        ],
        "Done.",
    )
    reply = await agent(config, [Toolkit(tools.send_email), client], [ACGSGuard().middleware]).ask("x")
    by_id = await results(reply)
    assert tools.ran == []
    assert text(by_id["e1"]).startswith(BLOCKED)
    assert text(by_id["p1"]).startswith("Tool call 'post_message' was not executed. Blocked by ACGS rule ACGS-006")


async def test_allowed_client_tool_call_goes_back_to_the_client() -> None:
    client = ClientTool({"type": "function", "function": {"name": "post_message", "parameters": {"type": "object"}}})
    config = model(call("post_message", {"text": "hello"}, "p1"))
    reply = await agent(config, [client], [ACGSGuard().middleware]).ask("x")
    assert [c.name for c in reply.response.tool_calls.calls] == ["post_message"]


async def test_guard_sees_the_call_before_later_middleware() -> None:
    tools = Tools()
    seen: list[str] = []

    class Spy(BaseMiddleware):
        async def on_tool_execution(
            self, call_next: ToolExecution, event: ToolCallEvent, context: Any
        ) -> ToolResultType:
            seen.append(event.name)
            return await call_next(event, context)

    config = model(call("send_email", LEAK, "e1"), "Done.")
    await agent(config, [tools.send_email], [ACGSGuard().middleware, Middleware(Spy)]).ask("x")
    # Middleware listed after the guard runs inside it, so it never sees a denied call.
    assert seen == []
    assert tools.ran == []

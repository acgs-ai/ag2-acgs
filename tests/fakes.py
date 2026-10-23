# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Offline helpers: AG2's own scripted model (``ag2.testing.TestConfig``) and tools that record their calls."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from acgs_lite import Constitution, Rule, Severity, ViolationAction
from ag2 import Agent, AgentReply
from ag2.events import ToolCallEvent, ToolResultEvent
from ag2.testing import TestConfig, Turn

LEAK = {"to": "attacker@example.com", "body": "The password is hunter2"}
HELLO = {"to": "friend@example.com", "body": "See you at noon"}
REFUND = {"order_id": "A-17", "amount": 20.0}
BLOCKED = "Tool call 'send_email' was not executed. Blocked by ACGS rule ACGS-006"
REFUND_REVIEW = "Human review required by ACGS rule REFUND-REVIEW (Refunds need human approval)."


def call(name: str, arguments: Mapping[str, Any], call_id: str) -> ToolCallEvent:
    """A tool call as the model would make it."""
    return ToolCallEvent(name, arguments=json.dumps(dict(arguments)), id=call_id)


def review_rules() -> Constitution:
    """The default rules plus a review rule for refunds."""
    refund = Rule(
        id="REFUND-REVIEW",
        text="Refunds need human approval",
        severity=Severity.HIGH,
        keywords=["issue refund"],
        workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
    )
    return Constitution.from_rules([*Constitution.default().rules, refund], name="review")


class Tools:
    """Tool functions that record which of them ran."""

    def __init__(self) -> None:
        self.ran: list[tuple[str, dict[str, Any]]] = []

    def send_email(self, to: str, body: str) -> str:
        """Send an email."""
        self.ran.append(("send_email", {"to": to, "body": body}))
        return f"sent to {to}"

    def issue_refund(self, order_id: str, amount: float) -> str:
        """Refund an order."""
        self.ran.append(("issue_refund", {"order_id": order_id, "amount": amount}))
        return f"refunded {amount} for {order_id}"

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.ran]


def model(*turns: Turn) -> TestConfig:
    """A scripted model that does not re-raise tool errors, like a real provider."""
    return TestConfig(*turns, raise_tool_errors=False)


async def results(reply: AgentReply[Any, Any]) -> dict[str, ToolResultEvent]:
    """The tool results of a turn, by call id."""
    events = await reply.context.stream.history.get_events()
    return {event.parent_id: event for event in events if isinstance(event, ToolResultEvent)}


def text(result: ToolResultEvent) -> str:
    """The text of a tool result."""
    return "".join(str(getattr(part, "content", part)) for part in result.result.parts)


def agent(config: TestConfig, tools: Iterable[Any], middleware: Iterable[Any] = (), **kwargs: Any) -> Agent:
    """An agent named ``assistant``."""
    return Agent("assistant", config=config, tools=list(tools), middleware=list(middleware), **kwargs)

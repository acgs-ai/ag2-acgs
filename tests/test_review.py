# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""Calls that need review are put to a human with ``context.ask(ToolApprovalRequest(...))``."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from ag2.events import HumanInputRequest, ToolApprovalRequest

from ag2_acgs import DECISION_METADATA_KEY, ACGSGuard
from tests.fakes import LEAK, REFUND, REFUND_REVIEW, Tools, agent, call, model, results, review_rules, text

NOT_RUN = "Tool call 'issue_refund' was not executed. "


class Reviewer:
    """A human-input hook that records the requests and gives a fixed answer."""

    def __init__(self, answer: str = "yes", delay: float = 0.0, error: Exception | None = None) -> None:
        self.answer = answer
        self.delay = delay
        self.error = error
        self.requests: list[HumanInputRequest] = []

    async def answer_request(self, event: HumanInputRequest) -> str:
        self.requests.append(event)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.answer

    @property
    def hook(self) -> Callable[[HumanInputRequest], Awaitable[str]]:
        """A plain function, which AG2's dependency injection can inspect."""

        async def hook(event: HumanInputRequest) -> str:
            return await self.answer_request(event)

        return hook


async def _refund(reviewer: Reviewer | None, guard: ACGSGuard | None = None) -> tuple[Tools, str, dict[str, object]]:
    tools = Tools()
    guard = guard or ACGSGuard(constitution=review_rules())
    config = model(call("issue_refund", REFUND, "r1"), "Done.")
    governed = agent(config, [tools.issue_refund], [guard.middleware])
    reply = (
        await governed.ask("Refund A-17", hitl_hook=reviewer.hook) if reviewer else await governed.ask("Refund A-17")
    )
    result = (await results(reply))["r1"]
    return tools, text(result), result.result.metadata


@pytest.mark.parametrize("answer", ["yes", "y", " YES ", "1", "Y\n"])
async def test_approved_call_runs(answer: str) -> None:
    reviewer = Reviewer(answer)
    tools, result, metadata = await _refund(reviewer)
    assert tools.names == ["issue_refund"]
    assert result == "refunded 20.0 for A-17"
    assert DECISION_METADATA_KEY not in metadata
    [request] = reviewer.requests
    assert isinstance(request, ToolApprovalRequest)
    assert request.tool_call_id == "r1"
    assert request.timeout is None
    assert request.content == (
        "ACGS: the agent wants to call the tool `issue_refund` with arguments "
        '{"order_id": "A-17", "amount": 20.0}.\n'
        f"{REFUND_REVIEW}\n"
        "Approve this call? (yes/no)"
    )


@pytest.mark.parametrize("answer", ["no", "n", "always", "", "yes please", "ok", "approve"])
async def test_any_other_answer_denies(answer: str) -> None:
    tools, result, metadata = await _refund(Reviewer(answer))
    assert tools.ran == []
    assert result == f"{NOT_RUN}{REFUND_REVIEW} The reviewer did not approve it."
    assert metadata[DECISION_METADATA_KEY] == {
        "verdict": "deny",
        "reason": f"{REFUND_REVIEW} The reviewer did not approve it.",
        "rule_ids": ["REFUND-REVIEW"],
        "warning_rule_ids": [],
        "error": None,
    }


async def test_no_human_input_hook_denies() -> None:
    tools, result, metadata = await _refund(None)
    assert tools.ran == []
    assert (
        result == f"{NOT_RUN}{REFUND_REVIEW} No approval was received (HumanInputNotProvidedError), so it was denied."
    )
    assert metadata[DECISION_METADATA_KEY]["error"].startswith("HumanInputNotProvidedError")  # type: ignore[index]


async def test_a_failing_hook_denies() -> None:
    tools, result, _ = await _refund(Reviewer(error=RuntimeError("pager down")))
    assert tools.ran == []
    assert "No approval was received (HumanInputFailedError)" in result


async def test_a_timeout_denies() -> None:
    reviewer = Reviewer("yes", delay=1.0)
    guard = ACGSGuard(constitution=review_rules(), review_timeout=0.05)
    tools, result, _ = await _refund(reviewer, guard)
    assert tools.ran == []
    assert "No approval was received (HumanInputTimeoutError)" in result
    assert reviewer.requests[0].timeout == 0.05


async def test_on_review_deny_never_asks() -> None:
    reviewer = Reviewer("yes")
    tools, result, _ = await _refund(reviewer, ACGSGuard(constitution=review_rules(), on_review="deny"))
    assert tools.ran == []
    assert reviewer.requests == []
    assert result == f"{NOT_RUN}{REFUND_REVIEW}"


async def test_blocking_rule_wins_and_nobody_is_asked() -> None:
    reviewer = Reviewer("yes")
    tools = Tools()
    leaky_refund = {"order_id": "A-17", "amount": 20.0, "note": "The password is hunter2"}
    config = model(call("issue_refund", leaky_refund, "r1"), "Done.")

    def issue_refund(order_id: str, amount: float, note: str) -> str:
        """Refund an order."""
        return tools.issue_refund(order_id, amount)

    reply = await agent(config, [issue_refund], [ACGSGuard(constitution=review_rules()).middleware]).ask(
        "x", hitl_hook=reviewer.hook
    )
    assert tools.ran == []
    assert reviewer.requests == []
    result = text((await results(reply))["r1"])
    assert result.startswith(f"{NOT_RUN}Blocked by ACGS rule ACGS-006")
    assert "REFUND-REVIEW" in result


async def test_each_call_is_asked_about_separately() -> None:
    reviewer = Reviewer("yes")
    tools = Tools()
    config = model(
        [call("issue_refund", REFUND, "r1"), call("send_email", LEAK, "e1")],
        call("issue_refund", REFUND, "r2"),
        "Done.",
    )
    governed = agent(
        config, [tools.issue_refund, tools.send_email], [ACGSGuard(constitution=review_rules()).middleware]
    )
    await governed.ask("x", hitl_hook=reviewer.hook)
    assert tools.names == ["issue_refund", "issue_refund"]
    assert [r.tool_call_id for r in reviewer.requests] == ["r1", "r2"]  # type: ignore[attr-defined]

# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""ACGS governance for AG2 agents: a middleware that checks each tool call before the tool runs."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Sequence
from typing import Any, Literal

from acgs_lite import Constitution
from ag2 import Context, Middleware
from ag2.events import (
    BaseEvent,
    BuiltinToolCallEvent,
    ModelResponse,
    ToolApprovalRequest,
    ToolCallEvent,
    ToolResult,
    ToolResultEvent,
)
from ag2.middleware import BaseMiddleware
from ag2.middleware.base import AgentTurn, LLMCall, ToolExecution, ToolResultType
from ag2.tools.builtin import (
    CodeExecutionTool,
    FileSearchTool,
    GoogleMapsTool,
    ImageGenerationTool,
    MCPServerTool,
    MemoryTool,
    RetrievalTool,
    ShellTool,
    SkillsTool,
    WebFetchTool,
    WebSearchTool,
    XSearchTool,
)
from ag2.tools.subagents.subagent_tool import SubagentTool
from ag2.utils import AGENT_CONTEXT_DEPENDENCY_KEY

from ag2_acgs._governance import GovernanceDecision, ToolCallFormatter, ToolCallGovernor, Verdict

logger = logging.getLogger(__name__)

DECISION_METADATA_KEY = "acgs_decision"
"""Key of the ACGS decision in the ``ToolResult.metadata`` of a call the middleware did not run."""

APPROVALS = frozenset({"y", "yes", "1"})
"""Replies to a review request that approve the call (case-insensitive, surrounding whitespace ignored).

These are the replies AG2's own ``approval_required`` accepts, minus ``always``: every call is checked on its own.
"""

# Provider-hosted tools: their ``register`` ignores agent middleware because the provider runs them.
_HOSTED_TOOLS: tuple[type[Any], ...] = (
    CodeExecutionTool,
    FileSearchTool,
    GoogleMapsTool,
    ImageGenerationTool,
    MCPServerTool,
    MemoryTool,
    RetrievalTool,
    ShellTool,
    SkillsTool,
    WebFetchTool,
    WebSearchTool,
    XSearchTool,
)
_EVALUATION_FAILED = "ACGS governance could not evaluate this tool call, so it was denied."
_NO_APPROVAL = "No approval was received ({error}), so it was denied."
_NOT_APPROVED = "The reviewer did not approve it."
_REVIEW_PROMPT = (
    "ACGS: the agent wants to call the tool `{name}` with arguments {arguments}.\n{reason}\nApprove this call? (yes/no)"
)


class ACGSConfigurationError(Exception):
    """Raised when ACGS cannot enforce its rules with the current configuration. It ends the turn."""


class UngovernedToolsError(ACGSConfigurationError):
    """Raised when a turn uses tools whose calls ACGS cannot check (see ``ACGSGuard.ungoverned_tools``)."""


def _describe_tool(tool: object, seen: set[int]) -> list[str]:
    if id(tool) in seen:
        return []
    seen.add(id(tool))
    name = getattr(tool, "name", type(tool).__name__)
    if isinstance(tool, _HOSTED_TOOLS):
        return [f"provider-hosted tool '{name}' ({type(tool).__name__})"]
    if isinstance(tool, SubagentTool):
        agent = tool.agent
        if (agent.tools or agent.tasks is not None) and not _has_acgs(agent.middleware):
            return [f"sub-agent tool '{name}' (agent '{agent.name}' runs its tools without ACGS middleware)"]
        return []
    nested = getattr(tool, "tools", None)
    if isinstance(nested, tuple):
        return [description for member in nested for description in _describe_tool(member, seen)]
    return []


def _has_acgs(middleware: Iterable[Any]) -> bool:
    for entry in middleware:
        factory = getattr(entry, "middleware", entry)
        if isinstance(factory, Middleware) and issubclass(factory.cls, ACGSMiddleware):
            return True
    return False


def _arguments(event: ToolCallEvent) -> Any:
    return json.loads(event.arguments or "{}")


def _metadata(decision: GovernanceDecision) -> dict[str, Any]:
    return {
        DECISION_METADATA_KEY: {
            "verdict": decision.verdict.value,
            "reason": decision.reason,
            "rule_ids": list(decision.rule_ids),
            "warning_rule_ids": list(decision.warning_rule_ids),
            "error": decision.error,
        }
    }


def _deny(decision: GovernanceDecision, reason: str, error: str | None) -> GovernanceDecision:
    return GovernanceDecision(
        verdict=Verdict.DENY,
        tool_name=decision.tool_name,
        reason=reason,
        rule_ids=decision.rule_ids,
        warning_rule_ids=decision.warning_rule_ids,
        error=error,
    )


class ACGSMiddleware(BaseMiddleware):
    """AG2 middleware that checks each tool call with an ``ACGSGuard`` before the tool runs.

    AG2 creates one instance per turn from ``guard.middleware``; do not create it directly.
    """

    def __init__(self, event: BaseEvent, context: Context, *, guard: ACGSGuard) -> None:
        """Create the middleware for one turn.

        Args:
            event: The event that started the turn.
            context: The turn's context.
            guard: The guard whose rules and options apply.
        """
        super().__init__(event, context)
        self.guard = guard

    async def on_turn(self, call_next: AgentTurn, event: BaseEvent, context: Context) -> ModelResponse:
        """Check the running agent's tools before the turn starts.

        Args:
            call_next: Runs the turn.
            event: The event that started the turn.
            context: The turn's context.

        Returns:
            The turn's model response.

        Raises:
            UngovernedToolsError: If the agent has tools ACGS cannot check and ``allow_ungoverned`` is off.
        """
        agent = context.dependencies.get(AGENT_CONTEXT_DEPENDENCY_KEY)
        if agent is not None:
            self.guard.check_agent(agent)
        return await call_next(event, context)

    async def on_llm_call(self, call_next: LLMCall, events: Sequence[BaseEvent], context: Context) -> ModelResponse:
        """Call the model and stop the turn if the provider ran one of its hosted tools during the call.

        Hosted tools run on the provider's side, so this detects them after the fact; it cannot prevent them.

        Args:
            call_next: Calls the model.
            events: The conversation sent to the model.
            context: The turn's context.

        Returns:
            The model response.

        Raises:
            UngovernedToolsError: If the provider ran a hosted tool and ``allow_ungoverned`` is off.
        """
        hosted: list[str] = []

        def record(event: BuiltinToolCallEvent) -> None:
            hosted.append(f"provider-hosted tool call '{event.name}' (already run by the provider)")

        with context.stream.where(BuiltinToolCallEvent).sub_scope(record, sync_to_thread=False):
            response = await call_next(events, context)
        if hosted:
            self.guard.report_ungoverned(hosted)
        return response

    async def on_tool_execution(
        self, call_next: ToolExecution, event: ToolCallEvent, context: Context
    ) -> ToolResultType:
        """Run the tool only if ACGS allows the call, or a human approved a call that needs review.

        This method never raises (AG2 drops the result of a call whose middleware raises): a call that is
        not run gets a ``ToolResultEvent`` with the reason, which the model reads.

        Args:
            call_next: Runs the rest of the middleware chain and the tool.
            event: The tool call.
            context: The turn's context.

        Returns:
            The tool's result, or a result explaining why the tool was not run.
        """
        guard = self.guard
        try:
            decision = guard.evaluate(event.name, _arguments(event))
        except Exception as error:
            decision = GovernanceDecision(
                verdict=Verdict.DENY,
                tool_name=event.name,
                reason=_EVALUATION_FAILED,
                error=f"{type(error).__name__}: {error}",
            )
        if decision.verdict is Verdict.ALLOW:
            return await call_next(event, context)
        if decision.verdict is Verdict.REVIEW and guard.on_review == "ask":
            decision = await self._review(event, context, decision)
            if decision.verdict is Verdict.ALLOW:
                return await call_next(event, context)
        return ToolResultEvent.from_call(
            event,
            result=ToolResult(
                f"Tool call '{decision.tool_name}' was not executed. {decision.reason}", metadata=_metadata(decision)
            ),
        )

    async def _review(self, event: ToolCallEvent, context: Context, decision: GovernanceDecision) -> GovernanceDecision:
        try:
            request = ToolApprovalRequest(
                _REVIEW_PROMPT.format(name=event.name, arguments=event.arguments, reason=decision.reason),
                tool_call_id=event.id,
                timeout=self.guard.review_timeout,
            )
            reply = await context.ask(request)
        except Exception as error:
            reason = f"{decision.reason} {_NO_APPROVAL.format(error=type(error).__name__)}"
            return _deny(decision, reason, f"{type(error).__name__}: {error}")
        if str(reply).strip().lower() in APPROVALS:
            return GovernanceDecision(
                verdict=Verdict.ALLOW,
                tool_name=decision.tool_name,
                reason=f"{decision.reason} Approved by the reviewer.",
                rule_ids=decision.rule_ids,
                warning_rule_ids=decision.warning_rule_ids,
            )
        return _deny(decision, f"{decision.reason} {_NOT_APPROVED}", None)


class ACGSGuard:
    """Check AG2 tool calls against an ACGS constitution before the tool runs.

    Pass ``guard.middleware`` to an agent (``Agent(..., middleware=[guard.middleware])``) or to one
    ``agent.ask(..., middleware=[...])`` call. For every tool call that runs in the agent's process
    (function tools, toolkits, local MCP tools, client tools, Anthropic bash) it:

    - runs allowed calls unchanged;
    - does not run blocked calls: the reason becomes the call's result, which the model reads;
    - asks a human through ``context.ask(ToolApprovalRequest(...))`` when only review rules match,
      and runs the call only on a ``yes``. No hook, a timeout, a failed hook or any other reply denies it.

    A blocking rule always wins over review rules, and any error while evaluating a call denies it.

    Before each turn it refuses agents with tools it cannot check (provider-hosted tools,
    ``tasks=TaskConfig(...)`` sub-tasks, sub-agents without ACGS middleware). Hosted tools passed for
    one ``ask`` are only detected after the provider ran them, and end the turn.

    Example:
        >>> from ag2_acgs import ACGSGuard
        >>> guard = ACGSGuard()
        >>> guard.evaluate("send_email", {"to": "a@example.com", "body": "The password is hunter2"}).verdict
        <Verdict.DENY: 'deny'>
    """

    def __init__(  # noqa: PLR0913
        self,
        *,
        constitution: Constitution | None = None,
        agent_id: str = "ag2",
        on_review: Literal["ask", "deny"] = "ask",
        review_timeout: float | None = None,
        allow_ungoverned: bool = False,
        tool_call_formatter: ToolCallFormatter | None = None,
    ) -> None:
        """Create the guard.

        Args:
            constitution: The ACGS constitution to enforce. Defaults to ``acgs_lite.Constitution.default()``.
            agent_id: Agent identifier recorded by ACGS for each validation.
            on_review: What to do when only review rules match. ``"ask"`` asks a human with
                ``context.ask(ToolApprovalRequest(...))``. ``"deny"`` denies the call.
            review_timeout: Seconds to wait for the human's answer, or ``None`` to wait as long as it takes.
                A timeout denies the call.
            allow_ungoverned: Log a warning instead of ending the turn when the agent uses tools ACGS cannot
                check.
            tool_call_formatter: Optional function ``(tool_name, args) -> str`` that replaces ``format_tool_call``.

        Raises:
            ValueError: If ``on_review`` or ``review_timeout`` is invalid.
        """
        if on_review not in ("ask", "deny"):
            msg = f"on_review must be 'ask' or 'deny', got {on_review!r}"
            raise ValueError(msg)
        if review_timeout is not None and not review_timeout > 0:
            msg = f"review_timeout must be positive or None, got {review_timeout!r}"
            raise ValueError(msg)
        self._governor = ToolCallGovernor(constitution, agent_id=agent_id, tool_call_formatter=tool_call_formatter)
        self.constitution: Constitution = self._governor.constitution
        self.agent_id = agent_id
        self.on_review = on_review
        self.review_timeout = review_timeout
        self.allow_ungoverned = allow_ungoverned
        self.middleware = Middleware(ACGSMiddleware, guard=self)
        """The middleware factory to pass in ``middleware=[...]``."""

    def __repr__(self) -> str:
        """Show the options, not the constitution."""
        return (
            f"ACGSGuard(agent_id={self.agent_id!r}, on_review={self.on_review!r}, "
            f"review_timeout={self.review_timeout!r}, allow_ungoverned={self.allow_ungoverned!r})"
        )

    def evaluate(self, tool_name: str, arguments: Any) -> GovernanceDecision:
        """Evaluate a tool call without running an agent. Never raises.

        Args:
            tool_name: Name of the tool.
            arguments: The tool arguments.

        Returns:
            The governance decision.
        """
        return self._governor.evaluate(tool_name, arguments)

    @staticmethod
    def ungoverned_tools(agent: Any) -> list[str]:
        """Describe what an agent can run that ACGS middleware never sees.

        Reported: provider-hosted tools from ``ag2.tools.builtin`` (except ``AnthropicBashTool`` and
        ``ToolSearchTool``, whose calls run locally through middleware), also inside toolkits;
        ``tasks=TaskConfig(...)``, because sub-task agents run the agent's tools without its middleware;
        and sub-agent tools whose agent has tools but no ACGS middleware.

        Args:
            agent: An ``ag2.Agent``.

        Returns:
            One description per ungoverned item, empty if everything is governed.
        """
        seen: set[int] = set()
        found = [description for tool in agent.tools for description in _describe_tool(tool, seen)]
        if agent.tasks is not None:
            found.append("sub-tasks (tasks=TaskConfig(...) runs this agent's tools in sub-agents without ACGS)")
        return found

    def check_agent(self, agent: Any) -> None:
        """Report what the agent can run without ACGS checks (see ``ungoverned_tools`` and ``report_ungoverned``).

        Args:
            agent: An ``ag2.Agent``.
        """
        ungoverned = self.ungoverned_tools(agent)
        if ungoverned:
            self.report_ungoverned(ungoverned)

    def report_ungoverned(self, descriptions: Sequence[str]) -> None:
        """Raise ``UngovernedToolsError``, or log a warning when ``allow_ungoverned`` is set.

        Args:
            descriptions: What ACGS cannot check.

        Raises:
            UngovernedToolsError: Unless ``allow_ungoverned`` is set.
        """
        listed = "; ".join(descriptions)
        if self.allow_ungoverned:
            logger.warning("ACGS does not check calls of: %s (allow_ungoverned is set).", listed)
            return
        msg = (
            f"ACGS cannot check calls of: {listed}. Remove them, or pass allow_ungoverned=True to run them "
            "without ACGS checks."
        )
        raise UngovernedToolsError(msg)

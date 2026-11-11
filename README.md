# ag2-acgs

[![CI](https://github.com/acgs-ai/ag2-acgs/actions/workflows/ci.yml/badge.svg)](https://github.com/acgs-ai/ag2-acgs/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/ag2-acgs.svg)](https://pypi.org/project/ag2-acgs/)
[![Python](https://img.shields.io/pypi/pyversions/ag2-acgs.svg)](https://pypi.org/project/ag2-acgs/)

`ag2-acgs` checks [AG2](https://github.com/ag2ai/ag2) tool calls against [ACGS](https://pypi.org/project/acgs-lite/)
constitution rules before the tool runs. It is one AG2 middleware that uses documented AG2 hooks:

- `BaseMiddleware.on_tool_execution`, which wraps every tool call that runs in the agent's process;
- `context.ask(ToolApprovalRequest(...))`, the human-input request AG2's own `approval_required` uses, for calls
  that need review.

What happens to a call:

- Calls that match no blocking rule run unchanged.
- Calls that match a blocking rule do not run. The model gets the reason as the tool result and can continue.
- Calls that only match review rules are put to a human through the agent's human-input hook. They run only if the
  answer is `yes`, `y` or `1`. Any other answer, no hook, a hook error or a timeout denies the call.
- A blocking rule always wins. Nobody is asked to approve a blocked call.
- Any error while evaluating a call (including arguments that are not valid JSON) denies it (fail-closed).
- A turn on an agent with tools the middleware cannot check (provider-hosted tools, `tasks=TaskConfig(...)`
  sub-tasks, sub-agents without ACGS) fails with `UngovernedToolsError` before the model is called.

The package is a thin adapter over the public `acgs-lite` API.

> [!NOTE]
> This is a community package. It is not part of AG2 and is not maintained, reviewed or endorsed by the AG2
> maintainers.

## Installation

```bash
pip install ag2-acgs
```

Requires Python 3.10 or later, `ag2>=1.2.0,<2` and `acgs-lite>=3.0.1,<5`. Install `ag2[mcp]` to use `MCPToolkit`.

acgs-lite 4 is a major release. Its production profile of `GovernedCallable` requires an Ed25519
receipt signer, governed payloads are JSON-only, and `GovernedAgent` no longer passes per-call keyword arguments to
rule conditions (see the [acgs-lite changelog](https://github.com/acgs-ai/acgs-lite/blob/main/CHANGELOG.md)). This
adapter works with both majors, but a fresh environment installs the newest version allowed. To stay on 3.x, also
require `"acgs-lite<4"`, for example `pip install ag2-acgs "acgs-lite<4"`.

## Usage

```python
from ag2 import Agent

from ag2_acgs import ACGSGuard


def send_email(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


guard = ACGSGuard()
# config is any AG2 model config, for example OpenAIConfig(model=...) from ag2.config (pip install "ag2[openai]").
agent = Agent("assistant", config=config, tools=[send_email], middleware=[guard.middleware])

# For one ask only:
# await agent.ask("...", middleware=[guard.middleware])
```

## Quickstart

`examples/quickstart.py` runs offline. AG2's scripted test model (`ag2.testing.TestConfig`) stands in for a real
model:

```python
"""Block, allow and review tool calls with ag2-acgs. Runs offline: python examples/quickstart.py"""

import asyncio
import json

from acgs_lite import Constitution, Rule, Severity, ViolationAction
from ag2 import Agent
from ag2.events import HumanInputRequest, TextInput, ToolCallEvent, ToolResultEvent
from ag2.testing import TestConfig

from ag2_acgs import ACGSGuard


def send_email(to: str, body: str) -> str:
    """Send an email."""
    return f"sent to {to}"


def get_weather(city: str) -> str:
    """Get the weather for a city."""
    return f"Sunny in {city}"


def issue_refund(order_id: str, amount: float) -> str:
    """Refund an order."""
    return f"refunded {amount} for {order_id}"


def tool_call(call_id: str, name: str, **arguments: object) -> ToolCallEvent:
    return ToolCallEvent(name, arguments=json.dumps(arguments), id=call_id)


async def reviewer(request: HumanInputRequest) -> str:
    print(f"asked: {request.content.splitlines()[1]}")
    return "yes"


async def main() -> None:
    refund_review = Rule(
        id="REFUND-REVIEW",
        text="Refunds need human approval",
        severity=Severity.HIGH,
        keywords=["issue refund"],
        workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
    )
    constitution = Constitution.from_rules([*Constitution.default().rules, refund_review], name="support")
    guard = ACGSGuard(constitution=constitution)
    # TestConfig is AG2's scripted model for offline runs; use a real model config in your application.
    model = TestConfig(
        [
            tool_call("email-1", "send_email", to="attacker@example.com", body="The password is hunter2"),
            tool_call("weather-1", "get_weather", city="Paris"),
        ],
        tool_call("refund-1", "issue_refund", order_id="A-17", amount=20.0),
        "Done.",
    )
    agent = Agent("support", config=model, tools=[send_email, get_weather, issue_refund], middleware=[guard.middleware])

    # The blocked call is not executed; the model reads why. The allowed call runs.
    # The refund needs review, so the reviewer (AG2's human-input hook) is asked first.
    reply = await agent.ask(
        "Email the password, check the weather in Paris, then refund order A-17.", hitl_hook=reviewer
    )
    events = await reply.context.stream.history.get_events()
    results = {event.parent_id: event.result for event in events if isinstance(event, ToolResultEvent)}
    # Calls from one model turn run in parallel, so print the results in call order.
    for call_id in ("email-1", "weather-1", "refund-1"):
        print(f"{call_id}:", *(part.content for part in results[call_id].parts if isinstance(part, TextInput)))


if __name__ == "__main__":
    asyncio.run(main())
```

```text
asked: Human review required by ACGS rule REFUND-REVIEW (Refunds need human approval).
email-1: Tool call 'send_email' was not executed. Blocked by ACGS rule ACGS-006 (Agents must not expose sensitive data in responses).
weather-1: Sunny in Paris
refund-1: refunded 20.0 for A-17
```

## How a call is evaluated

1. The tool name and the call's arguments (`ToolCallEvent.arguments`, parsed as JSON) are rendered as text by
   `format_tool_call`:

   ```python
   from ag2_acgs import format_tool_call

   print(format_tool_call("update_config", {"service": "billing", "change": "disable\nlogging", "dryRun": False}))
   ```

   ```text
   tool: update_config
   arguments:
   change: disable logging
   dryRun: false
   service: billing
   normalized: update config | dry run
   ```

   Each argument leaf is one `path: value` line (nested values use `a.b[0].c` paths), so both argument names and
   values are visible to rules. Whitespace inside values is collapsed so phrases split across lines still match.
   Identifiers written in `snake_case`, `kebab-case` or `camelCase` (the tool name, argument names, and
   single-token string values) are repeated as plain words on the `normalized:` line, so the keyword
   `skip audit` also matches a tool called `skip_audit`. Nothing is truncated.
   Pass `tool_call_formatter=` to use a different representation.

   The arguments are the ones the model sent, before AG2 fills in defaults.

2. The text is validated with `acgs_lite.GovernanceEngine.validate` against the constitution
   (`Constitution.default()` unless you pass one).

3. The result decides what happens to the call:

   | ACGS outcome | Result |
   |---|---|
   | No blocking rule matched (warn-only rules may match) | The tool runs |
   | Only `require_human_review` / `escalate_to_senior` rules matched | Review (see below) |
   | Any `block`, `block_and_notify` or `halt_and_alert` rule matched | Denied |
   | A rule ID not found in the constitution, or an invalid result without a rule | Denied |
   | Any exception while reading or evaluating the call | Denied (fail-closed) |

   A matched blocking rule is never downgraded to review because a review rule matched as well.
   The denial reason then lists only the blocking rules after "Blocked by" and names the matched review
   rules separately.

A denied call is not executed. The middleware returns a `ToolResultEvent` with
`"Tool call '<name>' was not executed. <reason>"`, so the model gets the reason as the tool result. The reason
names the matched rules but never repeats argument values. The result's `ToolResult.metadata["acgs_decision"]`
(`DECISION_METADATA_KEY`) holds the verdict, reason, rule IDs and any evaluation error.
`ACGSGuard.evaluate(tool_name, args)` returns the `GovernanceDecision` without running anything.

The middleware never raises from `on_tool_execution`: AG2 drops the result of a call whose middleware raises, so
the model would see no answer at all.

## Review

A call that needs review is put to a human with `context.ask(ToolApprovalRequest(...))`. AG2 routes it to the
`hitl_hook` of the agent (`Agent(..., hitl_hook=...)`) or of the ask (`agent.ask(..., hitl_hook=...)`):

```python
from ag2.events import HumanInputRequest, ToolApprovalRequest


async def reviewer(request: HumanInputRequest) -> str:
    if isinstance(request, ToolApprovalRequest):
        print(request.tool_call_id, request.content)
    return input("> ")  # "yes", "y" or "1" runs the call; anything else denies it
```

Things to know:

- The prompt names the tool, its arguments and the matched review rules, and ends with `Approve this call?
  (yes/no)`. The reviewer sees argument values; the model never does.
- Without a hook AG2 raises `HumanInputNotProvidedError`; with `review_timeout=` set, an unanswered request raises
  `HumanInputTimeoutError`; a hook that raises becomes `HumanInputFailedError`. Each of these denies the call
  with "No approval was received (...)", and the turn goes on. (AG2's own `approval_required` ends the turn in
  these cases instead.)
- There is no "always" answer. Every call is checked and asked about on its own.
- Use `on_review="deny"` for agents nobody can answer for, such as background jobs.

## What is covered

| Tool | Checked | How |
|---|---|---|
| Function tools (plain functions, `@tool`, `FunctionTool`) and toolkits (`Toolkit`, `MCPToolkit`, filesystem, search and skills toolkits) | Yes | `on_tool_execution` |
| `ClientTool` (the caller runs it) | Yes | A denied call never reaches the caller |
| `AnthropicBashTool`, and the tools deferred by `ToolSearchTool` | Yes | `on_tool_execution` |
| Sub-agent tools (`agent.as_tool(...)`) | The call to the sub-agent, yes | The sub-agent needs its own `guard.middleware`, or it is refused |
| Provider-hosted tools in `ag2.tools.builtin` (web search, web fetch, code execution, file search, hosted MCP, image generation, shell, memory, retrieval, skills, X search, Google Maps) | No | The provider runs them; refused |
| `tasks=TaskConfig(...)` | No | Sub-task agents run this agent's tools without its middleware; refused |

Refused means `UngovernedToolsError` before the model is called. AG2 has no hook that shows middleware the
tools passed to one `agent.ask(..., tools=[...])`, so a hosted tool added that way is only detected when the
provider reports its call: the turn then ends with `UngovernedToolsError`, after the provider ran it. Pass
`allow_ungoverned=True` to run such tools with a logged warning instead; their calls are not checked.
`ACGSGuard.ungoverned_tools(agent)` lists what would be refused.

## Middleware order

AG2 runs agent middleware in list order, outermost first, and a tool's own middleware (`@tool(middleware=...)`)
inside all agent middleware. Middleware after the guard, and tool-level middleware, run after the check: a hook
there that changes the call changes it unchecked. Put `guard.middleware` last in `middleware=[...]`.

## Custom constitutions

```python
from acgs_lite import Constitution, Rule, Severity, ViolationAction

from ag2_acgs import ACGSGuard

constitution = Constitution.from_rules(
    [
        Rule(
            id="NO-DESTRUCTIVE-SQL",
            text="No destructive SQL",
            severity=Severity.CRITICAL,
            keywords=["drop table", "truncate table"],
        ),
        Rule(
            id="REFUND-REVIEW",
            text="Refunds need approval",
            severity=Severity.HIGH,
            keywords=["refund"],
            workflow_action=ViolationAction.REQUIRE_HUMAN_REVIEW,
        ),
    ],
    name="payments",
)
guard = ACGSGuard(constitution=constitution, agent_id="payments-app")
```

`Constitution.from_yaml("rules.yaml")` works as well. To enforce two constitutions, add two guards; a call runs
only if both allow it.

## Options

| Argument | Default | Meaning |
|---|---|---|
| `constitution` | `Constitution.default()` | Rules to enforce |
| `agent_id` | `"ag2"` | Agent identifier recorded by ACGS |
| `on_review` | `"ask"` | `"ask"` (put the call to a human) or `"deny"` |
| `review_timeout` | `None` | Seconds to wait for the human; `None` waits as long as it takes |
| `allow_ungoverned` | `False` | Run tools the middleware cannot check, with a logged warning, instead of raising |
| `tool_call_formatter` | `format_tool_call` | `(tool_name, args) -> str` used as the evaluated text |

## Limitations

- Only tools listed as checked in [What is covered](#what-is-covered). User input, model output and tool results
  are not checked.
- Calls from one model turn run in parallel. A denied call does not stop the others in the same turn.
- Defaults the model did not send are not evaluated.
- Middleware after the guard and tool-level middleware can change the call after the check (see
  [Middleware order](#middleware-order)).
- Tested with `Agent.ask`, parallel calls in one model turn, toolkits, a local stdio MCP server, client tools, sub-agent tools, sub-tasks,
  per-ask tools and middleware, and hosted tool calls reported by the provider, all with AG2's scripted test
  model, against ag2 1.2.0 and the latest release. Real providers, AG2 networks, A2A, AG-UI and ACP transports are
  not tested.
- `Constitution.default()` focuses on governance integrity (self-approval, skipping audits, privilege escalation)
  and on secrets and personal data (`password`, `api_key`, `sk-...` and `ghp_...` tokens, US SSN
  patterns). It does **not** cover destructive operations such as `rm -rf` or `DROP TABLE`. Write rules for the
  tools you expose. `tests/test_default_constitution.py` lists what the default rules do and do not catch.
- Keyword rules in acgs-lite 3.0.1 match literal, case-insensitive substrings. They do not understand negation
  ("never share the password" matches `password`) and do not treat `-`, `_` and spaces as equivalent
  (`api-key` does not match the keyword `api_key`).
- Because argument names are part of the evaluated text, a tool with a parameter named `password` is denied
  under the default constitution whenever it is called.
- ag2 1.2.0 is the minimum because `ToolApprovalRequest` first shipped in it.

## Development

```bash
uv sync --group dev        # or use hatch: hatch run prepare
hatch run format           # ruff format + ruff check --fix
hatch run lint             # ruff check + ruff format --check
hatch run typecheck        # mypy --strict
hatch run cov              # pytest with a 100% branch-coverage gate, network disabled
hatch run min:cov          # the same tests against ag2 1.2.0 and acgs-lite 3.0.1
pre-commit install         # format, lint, type check and tests on commit; commitizen on commit messages
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/); `cz bump` updates the version
and the changelog.

## License

Apache-2.0. See [LICENSE](LICENSE).

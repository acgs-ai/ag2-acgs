"""Run one allowed and one denied AG2 tool call offline, then print the decision trail."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict
from typing import Any

# Disable telemetry before importing AG2; TestConfig never calls a model service.
os.environ["OTEL_SDK_DISABLED"] = "true"

from ag2 import Agent
from ag2.events import ToolCallEvent
from ag2.testing import TestConfig

from ag2_acgs import ACGSGuard, GovernanceDecision


class AuditedGuard(ACGSGuard):
    """Collect decisions through the public evaluator without retaining argument values."""

    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[GovernanceDecision] = []

    def evaluate(self, tool_name: str, arguments: Any) -> GovernanceDecision:
        decision = super().evaluate(tool_name, arguments)
        self.decisions.append(decision)
        return decision


def record_note(body: str) -> str:
    """Record a demo note by printing it locally."""
    print(f"Executed record_note: {body}")
    return "Note recorded."


async def main() -> None:
    calls = [
        ToolCallEvent("record_note", arguments=json.dumps({"body": body}), id=f"note-{index}")
        for index, body in enumerate(["The meeting starts at noon.", "The admin password is demo-secret."])
    ]
    guard = AuditedGuard()
    agent = Agent(
        "note-taker",
        config=TestConfig(*calls, "Done."),
        tools=[record_note],
        middleware=[guard.middleware],
    )
    await agent.ask("Record the two notes.")
    print("Audit trail (application decisions):")
    for decision in guard.decisions:
        print(json.dumps(asdict(decision), sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())

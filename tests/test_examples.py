# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
"""The README only shows code that runs, and the quickstart output it shows is the real output."""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from ag2_acgs import ACGSGuard
from tests.fakes import REFUND, Tools, agent, call, model, results, review_rules, text

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
QUICKSTART = ROOT / "examples" / "quickstart.py"


def _blocks(language: str) -> list[str]:
    return [textwrap.dedent(block) for block in re.findall(rf"```{language}\n(.*?)```", README, re.DOTALL)]


def test_quickstart_output_matches_readme() -> None:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-W", "error", str(QUICKSTART)],
        capture_output=True,
        text=True,
        check=True,
        cwd=ROOT,
        timeout=300,
    )
    [expected] = [block for block in _blocks("text") if block.startswith("asked:")]
    assert completed.stdout == expected
    assert completed.stderr == ""


def test_readme_embeds_the_quickstart_verbatim() -> None:
    assert QUICKSTART.read_text(encoding="utf-8") in _blocks("python")


def test_deny_and_audit_runs_offline_without_api_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key in list(os.environ):
        if key.endswith("API_KEY"):
            monkeypatch.delenv(key)
    # Register the example's import-time environment change so monkeypatch restores it.
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    # pytest-socket disables network access for this in-process example execution.
    runpy.run_path(str(ROOT / "examples" / "deny_and_audit.py"), run_name="__main__")
    output = capsys.readouterr().out
    execution, audit = output.split("Audit trail (application decisions):\n")
    assert execution == "Executed record_note: The meeting starts at noon.\n"
    decisions = [json.loads(line) for line in audit.splitlines()]
    assert [decision["verdict"] for decision in decisions] == ["allow", "deny"]
    assert [decision["tool_name"] for decision in decisions] == ["record_note", "record_note"]
    assert decisions[0]["rule_ids"] == []
    assert decisions[1]["rule_ids"] == ["ACGS-006"]
    assert "Blocked by" in decisions[1]["reason"]
    assert all(decision["error"] is None for decision in decisions)
    assert "demo-secret" not in output
    assert "The meeting starts at noon." not in audit


def test_readme_states_the_supported_versions() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for package in ("ag2", "acgs-lite"):
        [requirement] = re.findall(rf'"({package}>=[^"]+)"', pyproject)
        assert f"`{requirement}`" in README
        [floor] = re.findall(rf'"{package}==([^"]+)"', pyproject)
        assert requirement.startswith(f"{package}>={floor},")
    assert README.count("ag2 1.2.0") == 3


def test_every_readme_python_block_runs(capsys: pytest.CaptureFixture[str]) -> None:
    blocks = _blocks("python")
    assert len(blocks) == 5
    namespaces: list[dict[str, Any]] = []
    for block in blocks:
        namespace: dict[str, Any] = {"__name__": "readme", "config": model("Done.")}
        exec(compile(block, "<README>", "exec", dont_inherit=True), namespace)  # noqa: S102
        namespaces.append(namespace)
    assert "normalized: update config | dry run" in capsys.readouterr().out
    usage = namespaces[0]
    assert [entry.middleware for entry in usage["agent"].middleware] == [usage["guard"].middleware]
    assert isinstance(namespaces[-1]["guard"], ACGSGuard)


@pytest.mark.parametrize(("answer", "ran"), [("yes", ["issue_refund"]), ("no", [])])
async def test_readme_reviewer_answers_review_requests(
    answer: str, ran: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    [block] = [block for block in _blocks("python") if block.startswith("from ag2.events import HumanInputRequest")]
    namespace: dict[str, Any] = {"__name__": "readme"}
    exec(compile(block, "<README>", "exec", dont_inherit=True), namespace)  # noqa: S102
    monkeypatch.setattr("builtins.input", lambda prompt: answer)
    tools = Tools()
    governed = agent(
        model(call("issue_refund", REFUND, "r1"), "Done."),
        [tools.issue_refund],
        [ACGSGuard(constitution=review_rules()).middleware],
    )
    reply = await governed.ask("Refund A-17", hitl_hook=namespace["reviewer"])
    assert tools.names == ran
    assert "r1 ACGS: the agent wants to call the tool `issue_refund`" in capsys.readouterr().out
    assert text((await results(reply))["r1"])

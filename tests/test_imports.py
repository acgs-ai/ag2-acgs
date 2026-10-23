# SPDX-FileCopyrightText: 2026-present ACGS Contributors
#
# SPDX-License-Identifier: Apache-2.0
import ag2_acgs


def test_public_api() -> None:
    assert sorted(ag2_acgs.__all__) == sorted(
        [
            "APPROVALS",
            "DECISION_METADATA_KEY",
            "ACGSConfigurationError",
            "ACGSGuard",
            "ACGSMiddleware",
            "GovernanceDecision",
            "ToolCallFormatter",
            "UngovernedToolsError",
            "Verdict",
            "format_tool_call",
        ]
    )
    for name in ag2_acgs.__all__:
        assert getattr(ag2_acgs, name) is not None

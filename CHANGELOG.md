# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries for new releases are generated with `cz bump`.

## [Unreleased]

## 0.2.0 (unreleased)

### Added

- Offline `examples/deny_and_audit.py`: one allowed and one denied AG2 tool call, with an application decision trail.
- Compatibility tests for nested JSON-native arguments and bytes/bytearray formatting with acgs-lite 4.0.0.

### Changed

- Document the compatibility matrix: acgs-lite 4.0.0 is tested in the default environment, and the CI `test-min`
  job tests the 3.0.1 floor. Runtime support remains `acgs-lite>=3.0.1,<5`.
- Clarify that the adapter uses text-based validation and does not issue signed receipts.

## 0.1.0 (2026-10-23)

### Added

- `ACGSGuard` with `guard.middleware`: an AG2 middleware that checks each tool call's name and arguments against
  an ACGS constitution in `on_tool_execution`, before the tool runs, and returns the denial reason as the tool
  result.
- Review through AG2's human-input hook with `ToolApprovalRequest`; a missing hook, a failing hook or a timeout
  denies the call.
- Refusal of tools the middleware cannot check (provider-hosted tools, `tasks=`, sub-agents without the guard)
  before the model is called, and detection of hosted tool calls reported by the provider.
- `on_review="deny"`, `review_timeout=`, `allow_ungoverned=True` and `tool_call_formatter=` options.

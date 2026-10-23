# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries for new releases are generated with `cz bump`.

## [Unreleased]

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

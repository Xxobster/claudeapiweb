# SDK_NOTES (WP0, 2026-10-21)

What was checked against OFFICIAL docs on 2026-10-21 (research done by a read-only helper agent, then copied here; spot-check the key items again in WP5/WP8 before relying on them). "NOT VERIFIED" means do not build on it; check or ask first. Every later WP that uses an SDK/API fact cites a section here.

Sources: PyPI `claude-agent-sdk`; https://code.claude.com/docs/en/agent-sdk/{overview,python,cost-tracking}; https://code.claude.com/docs/en/authentication; https://platform.claude.com/docs/en/about-claude/{pricing,models/overview}; https://platform.claude.com/docs/en/build-with-claude/effort; https://platform.claude.com/docs/en/manage-claude/usage-cost-api; GitHub issues of `anthropics/claude-agent-sdk-python`.

## 1. Package (SDK-1)
- `claude-agent-sdk` 0.2.170 (released 2026-10-21), Python >= 3.10 (CPM uses 3.12). `pip install claude-agent-sdk`.
- The Claude Code CLI is bundled in the wheel and used by default; `cli_path` overrides. The machine also has Claude Code 2.1.289 installed separately.
- Windows: issue #1349 says PyPI lacked a win_amd64 wheel for 0.2.160-0.2.163; 0.2.170 lists one. **Pin the version in the lockfile and test the install on the PC (WP1/WP5).**
- NOT VERIFIED: Windows command-line length limit, asyncio subprocess behaviour on Windows, console window flashing.

## 2. Authentication and billing (SDK-2)
- Docs: third-party developers may not offer claude.ai login or subscription rate limits in their products; use API-key auth. SDK falls under the Commercial Terms. Whether Micah's own personal subscription may drive the SDK on his own PC is **NOT VERIFIED** -> CPM v1 is API-billed only; CLI/subscription mode stays an interface stub (Q9).
- Precedence (highest first): cloud-provider flags, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN`, profile credentials, subscription OAuth (`/login`). In non-interactive mode `ANTHROPIC_API_KEY` wins whenever present.
- Consequence: **an inherited `ANTHROPIC_API_KEY` silently turns a subscription session into API billing** (threat T-12). CPM builds the child env explicitly. Currently neither variable is set on Micah's PC (check_env.ps1).
- `ClaudeAgentOptions.env: dict[str,str]` sets child env. NOT VERIFIED: whether it merges with or replaces the parent environment -> CPM must also strip `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_USE_*` from the process environment it spawns from, and test this with a mock child that prints its env keys (WP5).
- Windows credential file: `%USERPROFILE%\.claude\.credentials.json` (never read or copied by CPM).

## 3. ClaudeAgentOptions (SDK-3)
Confirmed fields: `tools`, `allowed_tools`, `disallowed_tools`, `can_use_tool`, `permission_mode`, `hooks`, `permission_prompt_tool_name`, `system_prompt`, `mcp_servers`, `strict_mcp_config`, `continue_conversation`, `resume`, `session_id`, `fork_session`, `resume_session_at`, `max_turns`, `max_budget_usd`, `task_budget`, `model`, `fallback_model`, `effort`, `thinking`, `setting_sources`, `settings`, `cwd`, `cli_path`, `add_dirs`, `env`, `extra_args`, `sandbox`, `agents`, `skills`, `plugins`, `stderr`, `load_timeout_ms` (60000), `max_buffer_size`, `session_store`.
- `permission_mode`: `default`, `acceptEdits`, `plan`, `dontAsk`, `bypassPermissions`, `auto`. CPM default = `default` with explicit `allowed_tools` and a logging `can_use_tool`; `bypassPermissions` never default (rule 6, T-06).
- `effort`: `low|medium|high|xhigh|max`. `thinking`: adaptive / enabled(budget_tokens) / disabled (`max_thinking_tokens` deprecated). NOT VERIFIED: how `thinking` maps to the API for each model (see section 5).
- `max_budget_usd` counts only the current call's own spend; spend restored from a resumed session does not count against it. So it is a backstop only; the ledger (section 4) is authoritative.
- `setting_sources` values `user|project|local`. NOT VERIFIED: the default when None -> CPM sets it explicitly (project only for trusted workspaces, never `user` for workers).

## 4. Usage and cost (SDK-4)
- `ResultMessage`: `subtype, duration_ms, duration_api_ms, is_error, num_turns, session_id, stop_reason, total_cost_usd (optional), usage, result, model_usage, permission_denials, errors, api_error_status, uuid, terminal_reason`. `model_usage` per model: `inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens, costUSD, contextWindow, maxOutputTokens, costBasis`.
- `AssistantMessage` carries `usage`, `message_id`, `model`, `uuid`, `session_id`. Parallel tool calls in one turn share a `message_id`: **count each message_id once** (ledger dedup key).
- Per-step `output_tokens` is only a placeholder; read final output tokens from the result's `usage`/`model_usage`.
- `usage` = main loop only; `total_cost_usd`/`model_usage` include subagents.
- `total_cost_usd` and `costUSD` are client-side ESTIMATES from a bundled price table, not billing data -> UI labels them "estimate"; reconcile against the Console (section 6) when available.
- Error results carry cost too; on `error_max_budget_usd` the crossing response is in `total_cost_usd` but not in `usage`. After a crash cost fields may be zero.
- A resumed session's result includes earlier spend (restored from Claude Code v2.1.277); **never sum results across a resume**, store per-run deltas.
- Independent calls share no total; CPM sums them itself. Default cache TTL 5 min; `ENABLE_PROMPT_CACHING_1H` in `env` gives 1 h writes.

## 5. Models, prices, effort (SDK-5) — USD per million tokens
| Model | ID | Input | Cache write 5m | Cache write 1h | Cache read | Output | Default effort |
|---|---|---|---|---|---|---|---|
| Fable 5.1 | `claude-fable-5-1` | 10 | 12.50 | 20 | 0.25 | 50 | high |
| Opus 5.5 | `claude-opus-5-5` | 4 | 5 | 8 | 0.20 | 20 | medium |
| Sonnet 5.5 | `claude-sonnet-5-5` | 2 | 2.50 | 4 | 0.10 | 10 | high |
| Haiku 5.5 | `claude-haiku-5-5` | 0.10 (<=100k prompt) / 0.50 (>100k) | 0.125 / 0.625 | 0.20 / 1 | 0.01 / 0.05 | 0.50 / 2.50 | medium |
| Haiku 4.5 (legacy) | `claude-haiku-4-5-20251001` | 1 | 1.25 | 2 | 0.10 | 5 | n/a |
- Batch = 50% off. All four current models: 1M context, 128K max output, knowledge cutoff June 2026. All support effort low..max.
- Opus 5.5 returns HTTP 400 if thinking is disabled; Sonnet 5.5 does not accept `disabled`. NOT VERIFIED: whether a long-context surcharge applies beyond Haiku 5.5.
- Micah's global rule: Sonnet 5.5 default, Opus 5.5 only for the listed cases (user CLAUDE.md). Haiku 5.5 is the cheap default for planner/reviewer drafts if quality is proven. These prices seed the versioned DB price table (WP8); Micah can edit it; prices must be re-checked at every release.

## 6. Usage/Cost Admin API (SDK-6)
- Only for Console organizations with an Admin API key (`sk-ant-admin01-...`); not for individual accounts. `GET /v1/organizations/usage_report/messages` (1m/1h/1d buckets) and `GET /v1/organizations/cost_report` (daily, USD cents as strings; Priority Tier excluded). Data lags ~5 min.
- NOT VERIFIED: whether Micah's account is an organization. Until known, CPM relies on its own ledger plus a manual credit-balance entry (WP8); the Admin API reconciliation is optional and off by default. Admin key would be a second secret (higher privilege) -> not requested in v1.

## 7. Local tools verified on the PC (2026-10-21, scripts/check_env.ps1)
Unity `D:\Unity\Editors\6000.3.25f1\Editor\Unity.exe` exists. Blender 5.2.2 LTS at `C:\Program Files\Blender Foundation\Blender 5.2\blender.exe` (use `-b --python` for batch, as Nirandyr's `tools/art/*_build.py` do). The Unity batch command is taken from Nirandyr's `tools/run-editmode-tests.ps1`; it was not executed in WP0 (it would open the user's project).

## 8. Spot-check for WP5 (2026-10-26, `claude-agent-sdk` 0.2.170 installed in a scratch venv; signatures read from the package, no API call made)
- `ClaudeAgentOptions` is a dataclass whose fields include every option CPM uses: `cwd, model, effort, permission_mode, tools, allowed_tools, disallowed_tools, can_use_tool, setting_sources, max_turns, max_budget_usd, resume, env`. `effort` is `Literal["low","medium","high","xhigh","max"] | int`. `query(*, prompt: str | AsyncIterable, options)` yields `UserMessage | AssistantMessage | SystemMessage | ResultMessage | StreamEvent | ...`; `AssistantMessage` has `content, model, usage, message_id, session_id`; `ResultMessage` has `is_error, result, session_id, total_cost_usd, num_turns, subtype, errors, model_usage`. `PermissionResultAllow/Deny(message=...)` exist; `can_use_tool(tool_name, input, ToolPermissionContext)` is async.
- **Important**: a tool listed in `allowed_tools` is auto-approved and `can_use_tool` is NOT called for it (the SDK even warns, `CanUseToolShadowedWarning`). CPM therefore passes `allowed_tools=[]` and routes every call through `cpm.workers.permissions.decide`. `query()` always runs streaming internally, so `can_use_tool` works with a plain string prompt.
- `can_use_tool` makes the SDK set `permission_prompt_tool_name="stdio"`; combining it with an explicit `permission_prompt_tool_name` raises. CPM sets only `can_use_tool`.
- Still NOT VERIFIED (needs the first live test with Micah's go): behaviour of the bundled CLI on Windows, whether `ANTHROPIC_API_KEY` in the child environment is honoured when a subscription login also exists on the PC, real `usage` field names on `AssistantMessage.usage` (CPM reads `input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens` defensively), and the effective default of `setting_sources`.

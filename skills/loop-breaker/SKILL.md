---
description: Inspect, configure, or reset Loop Breaker — the runaway-loop guardrail. Use when the user asks about loop detection, runaway agent loops, why a tool call was blocked by Loop Breaker, or wants to change loop/budget thresholds.
disable-model-invocation: true
---

# Loop Breaker control

Loop Breaker is a PreToolUse hook that stops runaway loops (repeated near-identical
tool calls) before they waste tokens. This skill helps the user inspect and tune it.

## Where things live
- Hook script: `${CLAUDE_PLUGIN_ROOT}/hooks/loop_breaker.py`
- Per-session state: `~/.claude/loop-breaker/state/<session_id>.json`
- User config (optional): `~/.claude/loop-breaker/config.json`
- Project config (optional): `<project>/.loop-breaker.json`

## Common actions

**Show current config and defaults** — read `~/.claude/loop-breaker/config.json`
(if absent, the built-in defaults apply: kill mode, 5 identical calls in a row,
structural retry-storm detection, cycles up to period 6 ×4 with a read-only
exemption).

**Loosen / tighten detection** — edit the config file. Useful keys:
- `mode`: `"kill"` (block), `"warn"` (annotate, debounced), or `"off"`.
- `consecutive_threshold`: identical calls in a row that trip it (default 5).
- `structural_detection`: catch retries differing only in ids/timestamps/counters (default true).
- `cycle_reps` / `cycle_max_period`: short-cycle (A-B-A-B…) detection (default 4 / 6).
- `read_only_cycle_exempt`: don't trip cycles of pure inspection like `git status`/`git diff` (default true).
- `max_tool_calls` / `max_estimated_tokens`: optional budget backstops (0 = off).
- `ignore_tools`: tool names to never count or block.

**Reset a stuck session** — delete the session's state file under
`~/.claude/loop-breaker/state/`, or start a fresh Claude Code session.

**Temporarily disable** — set `mode` to `"off"` (or export `LOOP_BREAKER_MODE=off`).

Environment overrides exist for every key as `LOOP_BREAKER_<KEY>` (e.g.
`LOOP_BREAKER_CONSECUTIVE_THRESHOLD=8`).

When the user reports a false positive, prefer raising `consecutive_threshold` or
adding the tool to `ignore_tools` over disabling entirely.

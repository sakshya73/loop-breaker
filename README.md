# 🛑 Loop Breaker

**The runaway-loop kill switch for Claude Code.**

Loop Breaker is a tiny, open-source [PreToolUse hook](https://code.claude.com/docs/en/hooks)
that watches your agent's tool calls and **stops it the moment it gets stuck in a
loop** — the same edit retried over and over, the same failing command re-run, an
endless `read → run → read → run` cycle. It blocks the repeat call, tells the model
to change approach, and saves you from waking up to a burned budget.

It runs **in-harness, with no proxy** — nothing is routed through a third party,
there's no API gateway, and the whole thing is ~300 lines of dependency-free Python
you can read end to end before you trust it.

> **Why this exists.** Claude Code can hard-cap spend only in headless/print mode,
> not in the interactive sessions where overnight runaways actually happen, and it
> has **no loop detection at all** — the feature requests for it
> ([#4277](https://github.com/anthropics/claude-code/issues/4277),
> [#13996](https://github.com/anthropics/claude-code/issues/13996)) were both closed
> *"not planned."* People have reported burning **$6,000 overnight** and a single
> agent firing **14,000+ redundant calls**. Loop Breaker is the guardrail that fills
> that gap.

---

## What it catches

| Pattern | Example | Caught by |
|---|---|---|
| **Consecutive repeats** | the same `Edit` with the same `old_string` 5× in a row | `consecutive_threshold` |
| **Near-identical retries** | the same call tweaked by a space / counter each time | fuzzy matching (`fuzzy_threshold`) |
| **Short cycles** | `Read x.py → Bash make → Read x.py → Bash make → …` (same args each loop) | cycle detection |
| **Runaway spend** *(opt-in)* | a session blowing past a tool-call or estimated-token ceiling | budget backstops |

### What it deliberately does **not** flag

Productive iteration looks like a loop but isn't. Editing **different** files,
making **evolving** edits, or re-running tests **between real changes** all have
changing arguments — so their fingerprints differ and Loop Breaker leaves them
alone. It triggers on *stuck* repetition (identical arguments, no progress), which
keeps false positives near zero. When it's wrong, it **fails open** and when it
blocks, the block is **recoverable** — it never wedges your session.

---

## Install

**One command** (after the repo is on GitHub):

```shell
/plugin marketplace add sakshya73/loop-breaker
/plugin install loop-breaker@loop-breaker
```

**Try it locally first** (no install):

```shell
claude --plugin-dir /path/to/loop-breaker
```

Requires `python3` on your `PATH` (standard library only — no `pip install`).

---

## Configure

All settings are optional. Copy [`config.example.json`](./config.example.json) to
`~/.claude/loop-breaker/config.json` (global) or `<project>/.loop-breaker.json`
(per project), and override only what you want. Every key also has an
`LOOP_BREAKER_<KEY>` environment override.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"kill"` | `"kill"` blocks, `"warn"` only annotates, `"off"` disables |
| `consecutive_threshold` | `5` | near-identical calls in a row before tripping |
| `fuzzy_threshold` | `0.95` | similarity (0–1) counted as "the same call" |
| `cycle_reps` | `3` | repetitions of a short cycle to trip |
| `cycle_max_period` | `3` | longest cycle length to detect (≥2) |
| `window_size` / `window_threshold` | `0` / `0` | optional windowed-repeat detection (off) |
| `max_tool_calls` | `0` | optional hard ceiling on tool calls per session (off) |
| `max_estimated_tokens` | `0` | optional estimated-token backstop (off) |
| `ignore_tools` | `[]` | tool names to never count or block |

Hit a false positive? Raise `consecutive_threshold` or add the tool to
`ignore_tools` — don't reach for `"off"`.

---

## How it works

On every tool call, Claude Code sends the hook a JSON payload on stdin including the
`tool_name` and the exact `tool_input`. Loop Breaker:

1. fingerprints `(tool_name + canonical args)`,
2. compares it against a small **per-session history file**
   (`~/.claude/loop-breaker/state/<session_id>.json` — hooks run sequentially per
   session, so no transcript parsing and no races),
3. and if it sees a stuck pattern, returns
   `permissionDecision: "deny"` with a reason the model sees and can act on.

That's the whole trick. No network, no proxy, no telemetry.

---

## A note on budgets, and on Cost Guardian

A PreToolUse hook **can't see Claude Code's real token/cost numbers**, so Loop
Breaker's budget backstops are deliberately coarse *estimates* (a tool-call ceiling
plus an estimated-token tally), off by default. If you want **accurate** per-session
cost tracking, pair Loop Breaker with
[Cost Guardian](https://github.com/Manavarya09/cost-guardian) — it does budgets well
but doesn't do loop detection. **They're complementary:** Cost Guardian watches the
bill, Loop Breaker stops the loop that runs it up.

---

## Develop

```shell
python3 -m unittest discover -s tests -v   # run the test suite (zero deps)
```

The detection logic in `hooks/loop_breaker.py` is pure and unit-tested
(`tests/test_loop_breaker.py`). Contributions welcome — especially new stuck-loop
patterns and adapters for other hook-capable agents.

## License

[MIT](./LICENSE)

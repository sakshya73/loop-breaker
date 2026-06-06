#!/usr/bin/env python3
"""
Loop Breaker — the runaway-loop kill switch for Claude Code.

A PreToolUse hook that detects and stops runaway agent loops: N near-identical
consecutive tool calls, or short repeating cycles (A-B-A-B...) where each step
has the same arguments. It also carries a coarse, clearly-estimated budget
backstop (tool-call ceiling + estimated tokens) for completeness.

Design principles:
  * In-harness, no proxy, no network. You can read every line.
  * Fail OPEN: any unexpected error allows the tool call. A guardrail must never
    break a session.
  * Conservative by default: it triggers on *stuck* repetition (same arguments),
    not on productive iteration (different files / evolving edits), so false
    positives stay near zero.

Loop history is kept in a small per-session state file (hooks are sequential per
session, so there are no races). No transcript parsing required.

Reads the PreToolUse JSON payload on stdin. Emits a permission decision on
stdout per the Claude Code hook contract.
"""

import sys
import os
import json
import hashlib
import difflib

DEFAULTS = {
    "mode": "kill",                 # "kill" (deny) | "warn" (allow + note) | "off"
    "consecutive_threshold": 5,     # trip after N near-identical calls in a row
    "fuzzy_threshold": 0.95,        # similarity ratio (0-1) counted as "the same call"
    "cycle_reps": 3,                # repetitions of a short cycle needed to trip
    "cycle_max_period": 3,          # longest cycle period to look for (>= 2)
    "window_size": 0,               # windowed identical detection; 0 = disabled
    "window_threshold": 0,          # identical-call count within window to trip
    "max_tool_calls": 0,            # hard ceiling on tool calls per session; 0 = off
    "max_estimated_tokens": 0,      # rough estimated-token backstop; 0 = off
    "ignore_tools": [],             # tool names never counted or blocked
    "history_size": 60,             # recent calls retained in state
}

_ARG_EXCERPT = 2000  # chars of normalized args kept for fuzzy comparison


# --------------------------------------------------------------------------- #
# Pure logic (unit-tested directly)
# --------------------------------------------------------------------------- #

def canonical_args(tool_input):
    """Stable, order-independent string form of the tool arguments."""
    try:
        return json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(tool_input)


def fingerprint(tool_name, canon):
    """Content hash of (tool name + canonical args)."""
    h = hashlib.sha1()
    h.update((tool_name or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(canon.encode("utf-8", "replace"))
    return h.hexdigest()


def similarity(a, b):
    """Ratio in [0, 1] of how alike two argument strings are."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _matches(entry, fp, args, fuzzy):
    """Does a history entry represent the same call as (fp, args)?"""
    if entry.get("fp") == fp:
        return True
    return similarity(entry.get("args", ""), args) >= fuzzy


def _has_cycle(seq, period, reps):
    """True if `seq` ends with `reps` repetitions of a `period`-length block."""
    need = period * reps
    if len(seq) < need:
        return False
    block = seq[-period:]
    for r in range(1, reps):
        seg = seq[-period * (r + 1):-period * r]
        if seg != block:
            return False
    return True


def detect(history, fp, args, cfg):
    """
    Inspect recent history plus the incoming call.

    Returns (tripped: bool, kind: str, detail: str).
    `history` is a list of {"fp","args","tool"} oldest-first, NOT including the
    incoming call.
    """
    fuzzy = cfg["fuzzy_threshold"]

    # 1) Consecutive near-identical calls (the strongest stuck signal).
    n = cfg["consecutive_threshold"]
    if n and n > 0:
        consec = 1  # the incoming call
        for entry in reversed(history):
            if _matches(entry, fp, args, fuzzy):
                consec += 1
            else:
                break
        if consec >= n:
            return True, "consecutive", (
                f"{consec} near-identical calls in a row "
                f"(threshold {n})"
            )

    # 2) Short repeating cycle, e.g. A-B-A-B, where each position is stable.
    reps = cfg["cycle_reps"]
    max_p = cfg["cycle_max_period"]
    if reps and reps >= 2 and max_p and max_p >= 2:
        seq = [e.get("fp") for e in history] + [fp]
        for p in range(2, max_p + 1):
            if _has_cycle(seq, p, reps) and len(set(seq[-p:])) > 1:
                return True, "cycle", (
                    f"a {p}-step pattern repeated {reps}x with identical "
                    f"arguments each time"
                )

    # 3) Optional windowed repetition (off by default).
    w = cfg["window_size"]
    wt = cfg["window_threshold"]
    if w and w > 0 and wt and wt > 0:
        recent = history[-w:]
        count = 1 + sum(1 for e in recent if e.get("fp") == fp)
        if count >= wt:
            return True, "repeated", (
                f"the identical call appeared {count} times in the last "
                f"{w} calls (threshold {wt})"
            )

    return False, "", ""


def estimate_tokens(canon):
    """Very rough token estimate from argument size. Clearly an approximation."""
    return max(3, len(canon) // 4 + 3)


def coerce(value, like):
    """Coerce a string (from env) to the type of the default `like`."""
    if isinstance(like, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(float(value))
    if isinstance(like, float):
        return float(value)
    if isinstance(like, list):
        return [s.strip() for s in str(value).split(",") if s.strip()]
    return value


def load_config():
    """Merge defaults < user file < project file < environment."""
    cfg = dict(DEFAULTS)
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".claude", "loop-breaker", "config.json"),
    ]
    proj = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    candidates.append(os.path.join(proj, ".loop-breaker.json"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in cfg:
                        cfg[k] = v
        except FileNotFoundError:
            pass
        except Exception:
            pass  # malformed config must never break the hook
    # Environment overrides: LOOP_BREAKER_<KEY>
    for k, default in DEFAULTS.items():
        env = os.environ.get("LOOP_BREAKER_" + k.upper())
        if env is not None:
            try:
                cfg[k] = coerce(env, default)
            except Exception:
                pass
    return cfg


# --------------------------------------------------------------------------- #
# State (per session)
# --------------------------------------------------------------------------- #

def state_dir():
    override = os.environ.get("LOOP_BREAKER_STATE_DIR")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "loop-breaker", "state")


def _safe_session_id(session_id):
    keep = "-_."
    return "".join(c if (c.isalnum() or c in keep) else "_" for c in (session_id or "unknown"))[:128]


def load_state(session_id):
    path = os.path.join(state_dir(), _safe_session_id(session_id) + ".json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("calls", 0)
            data.setdefault("est_tokens", 0)
            data.setdefault("history", [])
            return data
    except Exception:
        pass
    return {"calls": 0, "est_tokens": 0, "history": []}


def save_state(session_id, state):
    d = state_dir()
    try:
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, _safe_session_id(session_id) + ".json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except Exception:
        pass  # persistence is best-effort; never break the session


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def emit_allow():
    sys.exit(0)


def emit_deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def emit_warn(note):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": note,
        }
    }))
    sys.exit(0)


def reason_text(kind, detail, tool_name):
    if kind == "budget":
        return (
            f"🛑 Loop Breaker: session budget backstop hit — {detail}. "
            "Stopping to prevent runaway spend. Raise the limit in "
            "~/.claude/loop-breaker/config.json or start a fresh session to reset."
        )
    return (
        f"🛑 Loop Breaker: this `{tool_name}` call looks like a stuck loop — "
        f"{detail}. Stopping to avoid wasting tokens/cost. Try a different "
        "approach (the repeated call wasn't making progress). Adjust "
        "thresholds in ~/.claude/loop-breaker/config.json, or set mode "
        '"warn"/"off" to disable blocking.'
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return emit_allow()

    cfg = load_config()
    if cfg.get("mode") == "off":
        return emit_allow()

    tool_name = payload.get("tool_name") or ""
    if tool_name in (cfg.get("ignore_tools") or []):
        return emit_allow()

    session_id = payload.get("session_id") or "unknown"
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = {}

    canon = canonical_args(tool_input)
    fp = fingerprint(tool_name, canon)
    args = canon[:_ARG_EXCERPT]
    est = estimate_tokens(canon)

    state = load_state(session_id)
    history = state.get("history", [])
    if not isinstance(history, list):
        history = []

    tripped, kind, detail = False, "", ""

    # Budget backstops (coarse; estimated).
    mtc = cfg.get("max_tool_calls", 0)
    if mtc and mtc > 0 and (state.get("calls", 0) + 1) > mtc:
        tripped, kind, detail = True, "budget", f"tool-call ceiling of {mtc} reached"
    if not tripped:
        met = cfg.get("max_estimated_tokens", 0)
        if met and met > 0 and (state.get("est_tokens", 0) + est) > met:
            tripped, kind, detail = True, "budget", (
                f"estimated-token backstop of {met} reached (~rough estimate)"
            )

    # Loop detection.
    if not tripped:
        tripped, kind, detail = detect(history, fp, args, cfg)

    # Update state. Append the incoming call to history.
    history.append({"fp": fp, "args": args, "tool": tool_name})
    hist_max = cfg.get("history_size", 60) or 60
    if len(history) > hist_max:
        history = history[-hist_max:]
    state["calls"] = state.get("calls", 0) + 1
    state["est_tokens"] = state.get("est_tokens", 0) + est

    if tripped and kind != "budget" and cfg.get("mode") == "kill":
        # Clear loop history so a changed approach isn't immediately re-tripped.
        history = []
    state["history"] = history
    save_state(session_id, state)

    if not tripped:
        return emit_allow()

    if cfg.get("mode") == "warn":
        return emit_warn(reason_text(kind, detail, tool_name))
    if cfg.get("mode") == "kill":
        return emit_deny(reason_text(kind, detail, tool_name))
    return emit_allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Fail open: never break a session because of the guardrail.
        sys.exit(0)

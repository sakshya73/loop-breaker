#!/usr/bin/env python3
"""
Loop Breaker — the runaway-loop kill switch for Claude Code.

A PreToolUse hook that detects and stops runaway agent loops:
  * the same call repeated (whitespace-insensitive),
  * near-identical retries that differ only in volatile fields
    (uuids / timestamps / counters) — i.e. retry storms,
  * short repeating cycles (A-B-A-B...) where each step has the same arguments.
It also carries a coarse, clearly-estimated budget backstop (off by default).

Design principles:
  * In-harness, no proxy, no network. You can read every line.
  * Fail OPEN: any unexpected error allows the tool call. A guardrail must never
    break a session.
  * Conservative by default: it triggers on *stuck* repetition (same arguments,
    no progress), never on productive iteration (different files / evolving
    edits / parameterized re-runs), so false positives stay near zero.

Loop history is kept in a small per-session state file, guarded by an advisory
lock so parallel tool calls in one session can't lose updates.

Reads the PreToolUse JSON payload on stdin. Emits a permission decision on
stdout per the Claude Code hook contract.
"""

import sys
import os
import re
import json
import hashlib
import contextlib

DEFAULTS = {
    "mode": "kill",                 # "kill" (deny) | "warn" (allow + note) | "off"
    "consecutive_threshold": 5,     # trip after N identical calls in a row (0 = off)
    "structural_detection": True,   # also catch retries differing only in volatile fields
    "cycle_reps": 4,                # repetitions of a short cycle needed to trip
    "cycle_max_period": 6,          # longest cycle period to look for (>= 2)
    "read_only_cycle_exempt": True, # don't trip cycles made only of read-only inspection
    "window_size": 0,               # windowed identical detection; 0 = disabled
    "window_threshold": 0,          # identical-call count within window to trip
    "max_tool_calls": 0,            # hard ceiling on tool calls per session; 0 = off
    "max_estimated_tokens": 0,      # rough estimated-token backstop; 0 = off
    "ignore_tools": [],             # tool names never counted or blocked
    "history_size": 60,             # recent calls retained in state
}

# Tools / commands that only READ state. Repeating these is investigation, not a
# runaway loop, so they're exempt from CYCLE detection (not from exact repeats).
READ_ONLY_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
READ_ONLY_BASH_PREFIXES = (
    "git status", "git diff", "git log", "git show", "git branch",
    "ls", "cat", "pwd", "head", "tail", "find", "grep", "rg", "echo",
    "which", "env", "whoami", "date", "tree", "wc", "stat", "file",
)

# Volatile fields whose changing value should NOT make a call look "new".
VOLATILE_KEYS = {
    "request_id", "requestid", "attempt", "attempt_id", "retry", "nonce",
    "idempotency_key", "ts", "timestamp", "time", "correlation_id",
    "trace_id", "span_id", "run_id", "job_id", "uuid", "guid",
}
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_LONGNUM_RE = re.compile(r"\d{10,}")   # timestamps, long ids, long digit runs
_WS_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Pure logic (unit-tested directly)
# --------------------------------------------------------------------------- #

def canonical_args(tool_input):
    """Stable, order-independent string form of the tool arguments."""
    try:
        return json.dumps(tool_input, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        return str(tool_input)


def _norm_ws(s):
    """Collapse whitespace so cosmetic-only differences vanish."""
    return _WS_RE.sub(" ", s).strip()


def fingerprint(tool_name, canon):
    """Content hash of (tool name + whitespace-normalized canonical args)."""
    h = hashlib.sha1()
    h.update((tool_name or "").encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(_norm_ws(canon).encode("utf-8", "replace"))
    return h.hexdigest()


def _scrub(value, key=None):
    """Replace volatile tokens so retries differing only in ids/timestamps match."""
    if key is not None and str(key).lower() in VOLATILE_KEYS:
        return "<v>"
    if isinstance(value, dict):
        return {k: _scrub(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return "<num>" if len(str(abs(value))) >= 10 else value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        s = _UUID_RE.sub("<uuid>", value)
        s = _LONGNUM_RE.sub("<num>", s)
        return s
    return value


def structural_fingerprint(tool_name, tool_input):
    """Fingerprint after stripping volatile fields — catches retry storms."""
    try:
        canon = json.dumps(_scrub(tool_input), sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        canon = canonical_args(tool_input)
    return fingerprint(tool_name, canon)


def is_read_only(tool_name, tool_input):
    """True if this call only inspects state (safe to repeat in a cycle)."""
    if tool_name in READ_ONLY_TOOLS:
        return True
    if tool_name == "Bash" and isinstance(tool_input, dict):
        cmd = str(tool_input.get("command", "")).strip()
        # Any shell operator could chain a mutating command — treat as not read-only.
        if any(op in cmd for op in ("&&", "||", ";", "|", ">", "<", "`", "$(")):
            return False
        low = cmd.lower()
        return any(low == p or low.startswith(p + " ") for p in READ_ONLY_BASH_PREFIXES)
    return False


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


def detect(history, fp, sfp, incoming_ro, cfg):
    """
    Inspect recent history plus the incoming call.

    Returns (tripped: bool, kind: str, detail: str, sig: str).
    `history` is a list of {"fp","sfp","tool","ro"} oldest-first, NOT including
    the incoming call. `sig` is a stable signature of the trigger (for debounce).
    """
    history = [e for e in history if isinstance(e, dict)]
    n = cfg["consecutive_threshold"]

    # 1) Consecutive identical calls (whitespace-normalized exact match).
    if n and n > 0:
        consec = 1
        for e in reversed(history):
            if e.get("fp") == fp:
                consec += 1
            else:
                break
        if consec >= n:
            return True, "consecutive", f"{consec} identical calls in a row (threshold {n})", fp

    # 2) Structural repeats: identical except for volatile fields (retry storms).
    if cfg.get("structural_detection") and n and n > 0:
        consec = 1
        for e in reversed(history):
            if e.get("sfp") == sfp:
                consec += 1
            else:
                break
        if consec >= n:
            return True, "structural", (
                f"{consec} near-identical calls in a row differing only in volatile "
                f"fields (ids/timestamps/counters)"
            ), "s:" + sfp

    # 3) Short repeating cycle, e.g. A-B-A-B, where each position is stable.
    reps = cfg["cycle_reps"]
    max_p = cfg["cycle_max_period"]
    if reps and reps >= 2 and max_p and max_p >= 2:
        seq = [e.get("fp") for e in history] + [fp]
        ro_seq = [bool(e.get("ro")) for e in history] + [incoming_ro]
        for p in range(2, max_p + 1):
            if _has_cycle(seq, p, reps) and len(set(seq[-p:])) > 1:
                window = p * reps
                if cfg.get("read_only_cycle_exempt") and all(ro_seq[-window:]):
                    continue  # benign read-only investigation, not a runaway
                return True, "cycle", (
                    f"a {p}-step pattern repeated {reps}x with identical arguments"
                ), "c:" + ":".join(seq[-p:])

    # 4) Optional windowed repetition (off by default).
    w = cfg["window_size"]
    wt = cfg["window_threshold"]
    if w and w > 0 and wt and wt > 0:
        recent = history[-w:]
        count = 1 + sum(1 for e in recent if e.get("fp") == fp)
        if count >= wt:
            return True, "repeated", (
                f"the identical call appeared {count} times in the last {w} calls "
                f"(threshold {wt})"
            ), fp

    return False, "", "", ""


def estimate_tokens(canon):
    """Very rough token estimate from argument size. Clearly an approximation."""
    return max(3, len(canon) // 4 + 3)


def coerce(value, like):
    """Coerce a value to the type of the default `like` (passes through if matching)."""
    if isinstance(like, bool):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int) and not isinstance(like, bool):
        if isinstance(value, bool):
            return int(value)
        return int(float(value))
    if isinstance(like, float):
        return float(value)
    if isinstance(like, list):
        if isinstance(value, list):
            return value
        return [s.strip() for s in str(value).split(",") if s.strip()]
    return value


def _clamp(cfg):
    """Coerce known keys into sane ranges so a bad config can't disable detection."""
    for k in ("consecutive_threshold", "cycle_reps", "cycle_max_period",
              "window_size", "window_threshold", "max_tool_calls",
              "max_estimated_tokens", "history_size"):
        try:
            v = int(cfg[k])
            cfg[k] = v if v >= 0 else DEFAULTS[k]
        except Exception:
            cfg[k] = DEFAULTS[k]
    for b in ("structural_detection", "read_only_cycle_exempt"):
        cfg[b] = bool(cfg.get(b, DEFAULTS[b]))
    if cfg["mode"] not in ("kill", "warn", "off"):
        cfg["mode"] = "kill"
    if not isinstance(cfg.get("ignore_tools"), list):
        cfg["ignore_tools"] = []
    # Couple history retention to the largest detectable cycle (avoid silent dead zone).
    cfg["history_size"] = max(cfg["history_size"], cfg["cycle_max_period"] * cfg["cycle_reps"], 1)
    return cfg


def load_config():
    """Merge defaults < user file < project file < environment, then clamp."""
    cfg = dict(DEFAULTS)
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".claude", "loop-breaker", "config.json")]
    proj = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    candidates.append(os.path.join(proj, ".loop-breaker.json"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for k, v in data.items():
                    if k in cfg:
                        try:
                            cfg[k] = coerce(v, DEFAULTS[k])
                        except Exception:
                            pass  # bad value -> keep default
        except FileNotFoundError:
            pass
        except Exception:
            pass  # malformed config must never break the hook
    for k, default in DEFAULTS.items():
        env = os.environ.get("LOOP_BREAKER_" + k.upper())
        if env is not None:
            try:
                cfg[k] = coerce(env, default)
            except Exception:
                pass
    return _clamp(cfg)


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
    sid = str(session_id) if session_id else "unknown"
    return "".join(c if (c.isalnum() or c in keep) else "_" for c in sid)[:128] or "unknown"


def load_state(session_id):
    path = os.path.join(state_dir(), _safe_session_id(session_id) + ".json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("calls", 0)
            data.setdefault("est_tokens", 0)
            hist = data.get("history", [])
            data["history"] = [e for e in hist if isinstance(e, dict)] if isinstance(hist, list) else []
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


@contextlib.contextmanager
def session_lock(session_id):
    """Best-effort advisory lock so parallel calls in one session don't race."""
    f = None
    try:
        d = state_dir()
        os.makedirs(d, exist_ok=True)
        f = open(os.path.join(d, _safe_session_id(session_id) + ".lock"), "w")
        try:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass  # no fcntl (e.g. Windows) or lock failure -> proceed unlocked
    except Exception:
        f = None
    try:
        yield
    finally:
        if f is not None:
            try:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass


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
    if not isinstance(payload, dict):
        return emit_allow()

    cfg = load_config()
    if cfg.get("mode") == "off":
        return emit_allow()

    tool_name = str(payload.get("tool_name") or "")
    if tool_name in (cfg.get("ignore_tools") or []):
        return emit_allow()

    session_id = str(payload.get("session_id") or "unknown")
    tool_input = payload.get("tool_input")
    if tool_input is None:
        tool_input = {}

    canon = canonical_args(tool_input)
    fp = fingerprint(tool_name, canon)
    sfp = structural_fingerprint(tool_name, tool_input)
    ro = is_read_only(tool_name, tool_input)
    est = estimate_tokens(canon)

    decision = "allow"
    kind = detail = ""

    with session_lock(session_id):
        state = load_state(session_id)
        history = state.get("history", [])

        tripped, sig = False, ""

        # Budget backstops (coarse; estimated; opt-in).
        mtc = cfg.get("max_tool_calls", 0)
        if mtc and mtc > 0 and (state.get("calls", 0) + 1) > mtc:
            tripped, kind, detail, sig = True, "budget", f"tool-call ceiling of {mtc} reached", "budget"
        if not tripped:
            met = cfg.get("max_estimated_tokens", 0)
            if met and met > 0 and (state.get("est_tokens", 0) + est) > met:
                tripped, kind, detail, sig = True, "budget", (
                    f"estimated-token backstop of {met} reached (rough estimate)"
                ), "budget"

        # Loop detection.
        if not tripped:
            tripped, kind, detail, sig = detect(history, fp, sfp, ro, cfg)

        # Decide. We do NOT clear history on a trip: an unchanged loop is blocked
        # every time, while a genuinely different next call breaks the streak and
        # is allowed immediately. warn mode is debounced by trigger signature.
        if tripped:
            if kind == "budget" or cfg.get("mode") == "kill":
                decision = "deny"
            elif cfg.get("mode") == "warn":
                if state.get("last_warned_sig") == sig:
                    decision = "allow"
                else:
                    state["last_warned_sig"] = sig
                    decision = "warn"

        # Record this call.
        history.append({"fp": fp, "sfp": sfp, "tool": tool_name, "ro": ro})
        hist_max = cfg.get("history_size", 60) or 60
        if len(history) > hist_max:
            history = history[-hist_max:]
        state["history"] = history
        state["calls"] = state.get("calls", 0) + 1
        state["est_tokens"] = state.get("est_tokens", 0) + est
        save_state(session_id, state)

    if decision == "deny":
        return emit_deny(reason_text(kind, detail, tool_name))
    if decision == "warn":
        return emit_warn(reason_text(kind, detail, tool_name))
    return emit_allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Fail open: never break a session because of the guardrail.
        sys.exit(0)

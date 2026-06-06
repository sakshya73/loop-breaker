#!/usr/bin/env bash
# Integration / delivery-confidence harness for Loop Breaker.
# Exercises the real hook (isolated state) for properties the unit tests don't cover:
# concurrency races, never-crash robustness, path-traversal safety, the mode matrix,
# corrupt-state recovery, bad-config resilience, and rough performance.
#
# Usage:  bash scripts/verify.sh
# Pairs with: python3 -m unittest discover -s tests -v
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
HOOK="$SCRIPT_DIR/../hooks/loop_breaker.py"
PASS=0; FAIL=0
ok(){ if eval "$2"; then echo "  PASS: $1"; PASS=$((PASS+1)); else echo "  FAIL: $1"; FAIL=$((FAIL+1)); fi; }
calls_in(){ python3 -c "import json;print(json.load(open('$1'))['calls'])" 2>/dev/null; }
decide(){ # payload -> deny|warn|allow|ERROR  (state dir from env)
  out=$(printf '%s' "$1" | python3 "$HOOK" 2>/tmp/lb_err)
  [ -s /tmp/lb_err ] && { echo ERROR; return; }
  [ -z "$out" ] && { echo allow; return; }
  printf '%s' "$out" | python3 -c 'import sys,json;d=json.load(sys.stdin)["hookSpecificOutput"];print(d.get("permissionDecision") or ("warn" if "additionalContext" in d else "?"))'
}

echo "== A. Concurrency / race: 12 parallel IDENTICAL calls (same description) =="
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"
P='{"session_id":"race","tool_name":"Bash","tool_input":{"command":"echo x","description":"same"}}'
for i in $(seq 1 12); do (printf '%s' "$P" | python3 "$HOOK" > "$LOOP_BREAKER_STATE_DIR/out_$i" 2>/dev/null) & done; wait
CALLS=$(calls_in "$LOOP_BREAKER_STATE_DIR/race.json")
DEN=$(grep -l deny "$LOOP_BREAKER_STATE_DIR"/out_* 2>/dev/null | wc -l | tr -d ' ')
echo "  recorded calls=$CALLS  denies=$DEN  (expect calls=12 = no lost updates)"
ok "no lost updates under concurrency (calls==12)" '[ "$CALLS" = "12" ]'
ok "loop caught under concurrency (>=1 deny)" '[ "${DEN:-0}" -ge 1 ]'

echo "== B. Robustness: never crash on garbage (exit 0 + no stderr traceback) =="
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"
PAYLOADS=(
'' 'not json at all' '[]' '"a bare string"' '123' 'null' '{}'
'{"tool_name":"Bash"}'
'{"tool_name":"Bash","tool_input":null}'
'{"tool_name":"Bash","tool_input":"notdict"}'
'{"tool_name":"Bash","tool_input":[1,2,3]}'
'{"tool_input":{"command":"x"}}'
'{"session_id":123,"tool_name":"Bash","tool_input":{"command":"x"}}'
'{"session_id":null,"tool_name":456,"tool_input":{"command":"x"}}'
'{"session_id":"../../etc/pwn","tool_name":"Bash","tool_input":{"command":"x"}}'
'{"tool_name":"X","tool_input":{"a":{"b":{"c":[1,{"d":2}]}}}}'
'{"tool_name":"mcp__srv__do","tool_input":{"q":"hi"}}'
)
crashes=0
for p in "${PAYLOADS[@]}"; do
  printf '%s' "$p" | python3 "$HOOK" >/dev/null 2>/tmp/lb_err; rc=$?
  if [ $rc -ne 0 ] || [ -s /tmp/lb_err ]; then crashes=$((crashes+1)); echo "    offender rc=$rc: $p :: $(cat /tmp/lb_err)"; fi
done
ok "all ${#PAYLOADS[@]} malformed payloads safe (exit 0, no traceback)" '[ "$crashes" = "0" ]'

BIG=$(python3 -c "print('x'*1000000)")
printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$BIG" | python3 "$HOOK" >/dev/null 2>/tmp/lb_err
ok "1MB command handled (exit 0, no traceback)" '[ $? = 0 ] && [ ! -s /tmp/lb_err ]'
printf '{"tool_name":"Bash","tool_input":{"command":"echo \xf0\x9f\x8e\x89 caf\xc3\xa9 \xe2\x98\x83"}}' | python3 "$HOOK" >/dev/null 2>/tmp/lb_err
ok "unicode/emoji handled (exit 0, no traceback)" '[ $? = 0 ] && [ ! -s /tmp/lb_err ]'

echo "== B2. Path-traversal session_id stays inside state dir =="
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"
printf '{"session_id":"../../pwn","tool_name":"Bash","tool_input":{"command":"x"}}' | python3 "$HOOK" >/dev/null 2>&1
inside=$(find "$LOOP_BREAKER_STATE_DIR" -maxdepth 1 -name '*.json' | wc -l | tr -d ' ')
escaped=$(ls "$(dirname "$LOOP_BREAKER_STATE_DIR")"/pwn.json 2>/dev/null | wc -l | tr -d ' ')
ok "state written inside dir, nothing escaped" '[ "$inside" -ge 1 ] && [ "$escaped" = "0" ]'

echo "== C. Mode matrix (6 identical SEQUENTIAL calls) =="
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)" LOOP_BREAKER_MODE=off
offdeny=0; for i in 1 2 3 4 5 6; do [ "$(decide "$P")" != allow ] && offdeny=$((offdeny+1)); done
ok "mode=off: never blocks (0 non-allow)" '[ "$offdeny" = "0" ]'
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)" LOOP_BREAKER_MODE=warn
warns=0; for i in 1 2 3 4 5 6; do [ "$(decide "$P")" = warn ] && warns=$((warns+1)); done
ok "mode=warn: warns exactly once (debounced)" '[ "$warns" = "1" ]'
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"; unset LOOP_BREAKER_MODE
seq_res=""; for i in 1 2 3 4 5 6; do seq_res="$seq_res$(decide "$P") "; done
echo "  kill sequence: $seq_res"
ok "mode=kill: 4th allows, 5th denies" '[ "$(echo $seq_res | cut -d" " -f4)" = allow ] && [ "$(echo $seq_res | cut -d" " -f5)" = deny ]'
ok "mode=kill: blocks every repeat after threshold (6th deny)" '[ "$(echo $seq_res | cut -d" " -f6)" = deny ]'

echo "== D. Corrupt / hostile state file recovery =="
RP='{"session_id":"r","tool_name":"Bash","tool_input":{"command":"x"}}'
chk_recover(){ # $1=label  $2=bad-state-content (no eval; args passed normally)
  d="$(mktemp -d)"; printf '%s' "$2" > "$d/r.json"
  printf '%s' "$RP" | LOOP_BREAKER_STATE_DIR="$d" python3 "$HOOK" >/dev/null 2>/tmp/lb_err
  rc=$?; c=$(calls_in "$d/r.json")
  if [ "$rc" = 0 ] && [ ! -s /tmp/lb_err ] && [ -n "$c" ] && [ "$c" -ge 1 ] 2>/dev/null; then
    echo "  PASS: $1 (recovered, calls=$c)"; PASS=$((PASS+1))
  else
    echo "  FAIL: $1 (rc=$rc calls='$c' err=$(cat /tmp/lb_err))"; FAIL=$((FAIL+1))
  fi
}
chk_recover "invalid JSON state recovers"     '{invalid json'
chk_recover "history-not-a-list recovers"     '{"history":"notalist","calls":5}'
chk_recover "non-dict history element dropped" '{"history":["stray",{"fp":"a","sfp":"a","tool":"X","ro":false}],"calls":2}'
chk_recover "null counters coerced"           '{"calls":null,"est_tokens":null,"history":[]}'

echo "== E. Bad config file does not disable detection =="
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"
proj="$(mktemp -d)"; printf '{"consecutive_threshold":"5","mode":"kill","cycle_max_period":-3}' > "$proj/.loop-breaker.json"
res=""; for i in 1 2 3 4 5; do res="$(CLAUDE_PROJECT_DIR="$proj" sh -c "printf '%s' '$P' | python3 '$HOOK'" 2>/dev/null)"; done
ok "string/negative config values still detect (5th denies)" 'printf "%s" "$res" | grep -q deny'

echo "== F. Performance (50 sequential invocations, startup-bound) =="
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"
start=$(python3 -c "import time;print(time.time())")
for i in $(seq 1 50); do printf '{"session_id":"perf","tool_name":"Read","tool_input":{"file_path":"f%d.py"}}' "$i" | python3 "$HOOK" >/dev/null 2>&1; done
end=$(python3 -c "import time;print(time.time())")
per=$(python3 -c "print(round(($end-$start)/50*1000,1))")
echo "  50 calls (~${per} ms/call, incl. python startup)"
ok "throughput sane (<150ms/call avg)" "python3 -c \"import sys;sys.exit(0 if ($end-$start)/50 < 0.15 else 1)\""

echo
echo "================= SUMMARY: $PASS passed, $FAIL failed ================="
[ "$FAIL" = "0" ] && { echo "ALL CLEAR"; exit 0; } || { echo "REVIEW FAILURES"; exit 1; }

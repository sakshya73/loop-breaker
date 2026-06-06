#!/usr/bin/env bash
# Loop Breaker — self-contained live demo (no install required).
# Drives the real hook with synthetic tool calls and shows the allow/block decisions.
# Great for an asciinema/GIF:  asciinema rec -c 'bash scripts/demo.sh'
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
HOOK="$SCRIPT_DIR/../hooks/loop_breaker.py"
B="\033[1m"; G="\033[32m"; R="\033[31m"; C="\033[36m"; D="\033[2m"; N="\033[0m"
SLEEP="${DEMO_SLEEP:-0}"   # set DEMO_SLEEP=0.3 for a readable screen-recording / GIF cadence
pause(){ [ "$SLEEP" != "0" ] && sleep "$SLEEP" 2>/dev/null || true; }

emit(){ # $1 = payload JSON, $2 = label
  out=$(printf '%s' "$1" | python3 "$HOOK" 2>/dev/null)
  python3 -c '
import sys,json
label,o = sys.argv[1], sys.argv[2].strip()
G="\033[32m"; R="\033[31m"; N="\033[0m"
if not o:
    print(f"   {label:<26} {G}allowed{N}")
else:
    d=json.loads(o)["hookSpecificOutput"]
    r=d.get("permissionDecisionReason","blocked")
    short=r.split("— ",1)[-1].split(". ",1)[0] if "— " in r else "blocked"
    print(f"   {label:<26} {R}BLOCKED{N} — {short}")
' "$2" "$out"
  pause
}

printf "${B}🛑 Loop Breaker${N} — the runaway-loop kill switch for Claude Code\n"
printf "${D}   live demo, no install needed (drives the real hook directly)${N}\n\n"

printf "${C}▶ 1. A stuck loop — the agent runs the SAME command over and over${N}\n"
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"
for i in 1 2 3 4 5 6; do
  emit '{"session_id":"d1","tool_name":"Bash","tool_input":{"command":"python deploy.py"}}' "call $i"
done

printf "\n${C}▶ 2. A retry storm — same call, a fresh request-id every time${N}\n"
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"
for i in 1 2 3 4 5 6; do
  emit "{\"session_id\":\"d2\",\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"deploy\",\"request_id\":\"$(uuidgen)\"}}" "attempt $i"
done

printf "\n${C}▶ 3. Real work is left alone — reading 5 DIFFERENT files${N}\n"
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"
for f in auth users billing search admin; do
  emit "{\"session_id\":\"d3\",\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"src/$f.ts\"}}" "read src/$f.ts"
done

printf "\n${C}▶ 4. A two-step cycle — build → read → build → read …${N}\n"
export LOOP_BREAKER_STATE_DIR="$(mktemp -d)"
for i in 1 2 3 4; do
  emit '{"session_id":"d4","tool_name":"Bash","tool_input":{"command":"npm run build"}}' "build (try $i)"
  emit '{"session_id":"d4","tool_name":"Read","tool_input":{"file_path":"src/app.ts"}}'   "read src/app.ts"
done

printf "\n${G}✓ Loop Breaker stops runaways, leaves real work alone.${N}\n"
printf "${D}  Install:${N} /plugin marketplace add sakshya73/loop-breaker  &&  /plugin install loop-breaker@loop-breaker\n"

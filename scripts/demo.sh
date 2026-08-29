#!/usr/bin/env bash
# Drive a demo take end to end. Two worlds, side by side on :8081.
#
#   scripts/demo.sh up           start all ten processes
#   scripts/demo.sh reset        clean slate: both worlds, limbo, attempt=1
#   scripts/demo.sh unprotected  agent -> :9001, no staging layer
#   scripts/demo.sh protected    agent -> :8080 through Limbo (effects held)
#   scripts/demo.sh retry        protected run with the CORRECTED tax ID
#   scripts/demo.sh approve      the door: commit + flush (needs verdict=pass)
#   scripts/demo.sh discard      manual purge (the failing path does this itself)
#   scripts/demo.sh state        what both worlds and limbo look like now
#   scripts/demo.sh ports        which of the 10 processes are up
#
# Add "replay" to any run to use a recorded transcript instead of the API:
#   scripts/demo.sh unprotected replay      (0 tokens, effects still real)
#
# A take is: reset -> unprotected -> protected -> retry -> approve.
set -u
cd "$(dirname "$0")/.." || exit 1

WP=http://127.0.0.1:9000    # protected tool server (behind Limbo)
WU=http://127.0.0.1:9001    # unprotected tool server
L=http://127.0.0.1:8080     # limbo
P1=http://localhost:8025    # protected   — people
P2=http://localhost:8026    # protected   — #procurement
U1=http://localhost:8035    # unprotected — people
U2=http://localhost:8036    # unprotected — #procurement

GOOD_TAX=ACME-88-4418   # the registry accepts anything but ACME-88-4417

count() { curl -s "$1/api/v1/messages" 2>/dev/null \
          | python -c 'import sys,json;print(json.load(sys.stdin)["total"])' 2>/dev/null \
          || echo "?"; }

up() {
  # Mailpit MUST carry --api-cors or the panel's inbox panes stay silently
  # empty — and an empty protected inbox looks correct, so it fails quietly.
  bin/mailpit.exe --listen 0.0.0.0:8025 --smtp 0.0.0.0:1025 \
      --database data/mail1.db --api-cors "*" >/dev/null 2>&1 &
  bin/mailpit.exe --listen 0.0.0.0:8026 --smtp 0.0.0.0:1026 \
      --database data/mail2.db --api-cors "*" >/dev/null 2>&1 &
  bin/mailpit.exe --listen 0.0.0.0:8035 --smtp 0.0.0.0:1035 \
      --database data/mail3.db --api-cors "*" >/dev/null 2>&1 &
  bin/mailpit.exe --listen 0.0.0.0:8036 --smtp 0.0.0.0:1036 \
      --database data/mail4.db --api-cors "*" >/dev/null 2>&1 &
  python world/registry.py >/dev/null 2>&1 &                       # :7000, shared
  python world/tools.py    >/dev/null 2>&1 &                       # :9000 protected
  LIMBO_DB=data/unprotected.db LIMBO_SMTP_MAIN=1035 \
    LIMBO_SMTP_CHANNEL=1036 LIMBO_TOOLS_PORT=9001 \
    python world/tools.py  >/dev/null 2>&1 &                       # :9001 unprotected
  sleep 6
  python limbo/server.py   >/dev/null 2>&1 &                       # :8080
  python limbo/ui/serve.py >/dev/null 2>&1 &                       # :8081
  sleep 5
  ports
  echo "panel: http://127.0.0.1:8081"
}

reset() {
  curl -s -X POST    "$WP/txn/reset"       >/dev/null
  curl -s -X POST    "$WU/txn/reset"       >/dev/null
  curl -s -X POST    "$L/reset?full=1"     >/dev/null
  for m in "$P1" "$P2" "$U1" "$U2"; do
    curl -s -X DELETE "$m/api/v1/messages" >/dev/null
  done
  echo "reset: both worlds, limbo, attempt counter, and all four inboxes clear"
}

# $1 = MCP_URL, $2 = transcript label, $3 = "replay" or empty
run() {
  if [ "${3:-}" = "replay" ]; then
    MCP_URL="$1" python agent/run.py --replay "$2" 2>&1 \
      | grep -viE "deprecat|create_react_agent"
    return
  fi
  # GROQ_API_KEY lives in .env and nothing loads it automatically.
  [ -f .env ] && { set -a; . ./.env; set +a; }
  [ -z "${GROQ_API_KEY:-}" ] && { echo "GROQ_API_KEY not set (.env missing?)"; exit 1; }
  MCP_URL="$1" python agent/run.py --record "$2" 2>&1 \
    | grep -viE "deprecat|create_react_agent"
}

# Beat 2: the operator corrects the tax ID and runs the same agent again. The
# attempt counter goes up; the world is still frozen from attempt 1.
retry() {
  curl -s -X POST "$WP/txn/reset" >/dev/null
  curl -s -X POST "$L/reset"      >/dev/null   # no ?full — this is attempt 2
  echo "retrying with corrected tax ID $GOOD_TAX"
  LIMBO_TAX_ID="$GOOD_TAX" run http://127.0.0.1:8080/mcp attempt2 "${1:-}"
}

approve() {
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$L/commit")
  if [ "$code" = "409" ]; then
    echo "REFUSED — commit needs a passing verdict (this is the gate working)"
  else
    echo "approved: transaction committed, held effects flushed"
  fi
}

state() {
  echo "unprotected : inbox=$(count $U1)+$(count $U2)"
  echo -n "              db "; curl -s "$WU/state"; echo
  echo "protected   : inbox=$(count $P1)+$(count $P2)"
  echo -n "              db "; curl -s "$WP/state"; echo
  echo -n "limbo       : "
  curl -s "$L/state" | python -c '
import sys, json
d = json.load(sys.stdin)
print("attempt=%d  verdict=%s  held=%d  outcome=%s"
      % (d["attempt"], d["verdict"], d["count"], d["outcome"] or "-"))
for h in d["held"]:      print("   HELD     %-14s -> %s" % (h["tool"], h["target"]))
for f in d["flushed"]:   print("   SENT     %-14s -> %s" % (f["tool"], f["target"]))
for f in d["forwarded"]: print("   FORWARD  %-14s (%s)" % (f["tool"], f["kind"]))
' 2>/dev/null || echo "(limbo not running)"
}

ports() {
  for p in 7000 8025 8026 8035 8036 8080 8081 9000 9001; do
    pid=$(netstat -ano | grep ":$p " | grep LISTEN | awk '{print $5}' | head -1)
    if [ -n "$pid" ]; then printf "  %-5s up (pid %s)\n" "$p" "$pid"
    else                   printf "  %-5s DOWN\n" "$p"; fi
  done
}

case "${1:-}" in
  up)          up ;;
  reset)       reset ;;
  unprotected) run http://127.0.0.1:9001/mcp unprotected "${2:-}" ;;
  protected)   run http://127.0.0.1:8080/mcp attempt1     "${2:-}" ;;
  retry)       retry "${2:-}" ;;
  approve)     approve ;;
  discard)     curl -s -X POST "$L/discard" >/dev/null && echo "discarded" ;;
  state)       state ;;
  ports)       ports ;;
  *)           sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//' ;;
esac

#!/bin/zsh
# TMT Radar — scheduled local sweep. Zero LLM. Fails loud:
# a FAILED source keeps a non-zero exit; a tripwire WARN (sequence gap, shelf
# shrink, staleness, drift, crosscheck catch) also raises a notification, because
# a sequence gap can mean a missed instrument even when every fetch succeeded.
set -u
DIR="${0:A:h}"
PY="$DIR/.venv/bin/python"
LOG="$DIR/sweep.log"
OUT="$(mktemp)"

echo "==== sweep $(date '+%Y-%m-%d %H:%M:%S') ====" >> "$LOG"
"$PY" "$DIR/tracker.py" sweep > "$OUT" 2>&1
code=$?
cat "$OUT" >> "$LOG"

# keep the dashboard data file current after every sweep
"$PY" "$DIR/tracker.py" export >> "$LOG" 2>&1

NEW=$(grep -c '^  NEW ' "$OUT")
WARNS=$(grep -c '^  WARN ' "$OUT")

if [ $code -ne 0 ]; then
  osascript -e 'display notification "A source FAILED — open engine/health.json" with title "TMT Radar" sound name "Basso"' 2>/dev/null
elif [ "$WARNS" -gt 0 ]; then
  osascript -e "display notification \"$WARNS tripwire warning(s) — check engine/health.json\" with title \"TMT Radar\" sound name \"Basso\"" 2>/dev/null
fi
if [ "$NEW" -gt 0 ]; then
  osascript -e "display notification \"$NEW new substantive instrument(s) detected\" with title \"TMT Radar\"" 2>/dev/null
fi
rm -f "$OUT"
exit $code

#!/bin/zsh
# TMT Radar — one sweep, on demand.
#
# There is deliberately no schedule. Updates happen when someone presses "Check all
# sources now" in the console, or runs this script. That is a considered trade: the
# tracker never fetches unattended, but it also never refreshes itself, so the partner
# dashboard carries a staleness banner that appears once the data is more than a day old.
# Nothing here silently pretends to be current.
set -u
DIR="${0:A:h}"
PY="$DIR/.venv/bin/python"
LOG="$DIR/sweep.log"
OUT="$(mktemp)"

echo "==== sweep $(date '+%Y-%m-%d %H:%M:%S') ====" >> "$LOG"
"$PY" "$DIR/tracker.py" sweep > "$OUT" 2>&1
code=$?
cat "$OUT" >> "$LOG"
"$PY" "$DIR/tracker.py" export >> "$LOG" 2>&1
"$PY" "$DIR/../code/build_dashboard_v2.py" >> "$LOG" 2>&1

NEW=$(grep -c '^  NEW ' "$OUT")
WARNS=$(grep -cE '^  (WARN|EMPTY) ' "$OUT")
if [ $code -ne 0 ]; then
  osascript -e 'display notification "A source FAILED — open engine/health.json" with title "TMT Radar" sound name "Basso"' 2>/dev/null
elif [ "$WARNS" -gt 0 ]; then
  osascript -e "display notification \"$WARNS source warning(s) — check engine/health.json\" with title \"TMT Radar\" sound name \"Basso\"" 2>/dev/null
fi
[ "$NEW" -gt 0 ] && osascript -e "display notification \"$NEW new substantive instrument(s)\" with title \"TMT Radar\"" 2>/dev/null
cat "$OUT"; rm -f "$OUT"
exit $code

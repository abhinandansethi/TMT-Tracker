#!/bin/zsh
# TMT Radar — scheduled local sweep. Zero LLM. Fails loud:
# any source failure or tripwire puts a macOS notification on screen and keeps exit non-zero.
set -u
DIR="${0:A:h}"
PY="$DIR/.venv/bin/python"
LOG="$DIR/sweep.log"

echo "==== sweep $(date '+%Y-%m-%d %H:%M:%S') ====" >> "$LOG"
"$PY" "$DIR/tracker.py" sweep >> "$LOG" 2>&1
code=$?

# keep the dashboard data file current after every sweep
"$PY" "$DIR/tracker.py" export >> "$LOG" 2>&1

if [ $code -ne 0 ]; then
  osascript -e 'display notification "A source FAILED or a tripwire fired — open engine/health.json" with title "TMT Radar" sound name "Basso"' 2>/dev/null
else
  # surface new substantive items, if any, from the tail of the log
  NEW=$(tail -40 "$LOG" | grep -c '^  NEW ')
  if [ "$NEW" -gt 0 ]; then
    osascript -e "display notification \"$NEW new substantive instrument(s) detected\" with title \"TMT Radar\"" 2>/dev/null
  fi
fi
exit $code

#!/bin/zsh
# Double-click to start the TMT Radar connector API (localhost:8788).
# The partner's pipeline calls this to pull instruments; see docs/CONNECTOR.md.
cd "${0:A:h}"
exec .venv/bin/python radar_api.py

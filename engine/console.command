#!/bin/zsh
# Double-click this file to open the TMT Radar operator console.
cd "${0:A:h}"
open "http://127.0.0.1:8787"
exec .venv/bin/python console.py

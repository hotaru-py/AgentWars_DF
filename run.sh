#!/usr/bin/env bash
# Thin wrapper; the real launcher is run.py (works on macOS, Linux and Windows).
cd "$(dirname "$0")" && exec "$(command -v python3 || command -v python)" run.py "$@"

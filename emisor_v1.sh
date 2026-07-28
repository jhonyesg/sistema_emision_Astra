#!/bin/bash
echo -ne "\033]0;EMISOR V1\007"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
export ASTRA_INI="db/1_default.ini"
exec python3 "$SCRIPT_DIR/app.py"

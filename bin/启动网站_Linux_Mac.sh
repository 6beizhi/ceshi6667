#!/bin/sh
cd "$(dirname "$0")/.." || exit 1
HOST=127.0.0.1 PORT=8000 python3 app.py

#!/bin/sh
set -e

# main.py itself owns the 30-minute retry loop, so this wrapper only launches it once.
exec python3 src/main.py

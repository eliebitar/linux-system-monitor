#!/bin/bash
# Start SysMon on port 3200
cd "$(dirname "$0")"
echo "Starting SysMon..."
exec ./venv/bin/python server.py

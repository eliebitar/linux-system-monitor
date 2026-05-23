#!/bin/bash
# Start SysMon on port 3200
cd "$(dirname "$0")"
echo "Starting SysMon..."
python3 server.py

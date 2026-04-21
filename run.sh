#!/usr/bin/env bash
# CUCM Tools launcher for Linux / macOS
# Usage: ./run.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

# Create virtual environment if it doesn't exist
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "Setting up virtual environment for the first time..."
    python3 -m venv "$VENV_DIR"
fi

# Activate venv
source "$VENV_DIR/bin/activate"

# Install / update dependencies
echo "Checking dependencies..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"

cd "$SCRIPT_DIR"
python3 launch.py

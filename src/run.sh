#!/bin/bash
# Dashboard metrics collection and generation script
# Called by cron monthly, or manually for first run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="/opt/dashboard"
VENV_DIR="$BASE_DIR/venv"
WEB_ROOT="/var/www/dashboard"
LOG_PREFIX="[dashboard-metrics]"

echo "$LOG_PREFIX $(date -u '+%Y-%m-%dT%H:%M:%SZ') Starting collection run..."

# Load environment
if [ -f "$BASE_DIR/.env" ]; then
    export $(grep -v '^#' "$BASE_DIR/.env" | xargs)
fi

# Activate virtual environment
source "$VENV_DIR/bin/activate"

# Run collector
echo "$LOG_PREFIX Running collector..."
cd "$SCRIPT_DIR"
python3 collector.py
if [ $? -ne 0 ]; then
    echo "$LOG_PREFIX ERROR: Collector failed!"
    exit 1
fi

# Run generator
echo "$LOG_PREFIX Running generator..."
python3 generator.py
if [ $? -ne 0 ]; then
    echo "$LOG_PREFIX ERROR: Generator failed!"
    exit 1
fi

# Deploy to web root
echo "$LOG_PREFIX Deploying to web root..."
mkdir -p "$WEB_ROOT"
rsync -a --delete "$BASE_DIR/output/" "$WEB_ROOT/"

echo "$LOG_PREFIX $(date -u '+%Y-%m-%dT%H:%M:%SZ') Run complete."

#!/bin/bash
# ============================================================================
# Upgrade Tracker — Update Script
# ============================================================================
# Deploys new code without touching config, database, or OAuth tokens.
#
# Usage:
#   scp -i <key.pem> -r deployment-deepesh-server/ <user>@<ec2-ip>:/tmp/tracker-deploy/
#   ssh -i <key.pem> <user>@<ec2-ip>
#   cd /tmp/tracker-deploy
#   sudo bash update.sh
# ============================================================================

set -euo pipefail

APP_NAME="upgrade-tracker"
APP_DIR="/opt/${APP_NAME}"
APP_USER="tracker"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Updating ${APP_NAME}..."

# Copy Python files
for f in server.py db.py gcal.py seed.py; do
    if [ -f "${SCRIPT_DIR}/app/${f}" ]; then
        cp "${SCRIPT_DIR}/app/${f}" "${APP_DIR}/${f}"
        echo "  Updated ${f}"
    fi
done

# Copy frontend
if [ -f "${SCRIPT_DIR}/app/static/index.html" ]; then
    cp "${SCRIPT_DIR}/app/static/index.html" "${APP_DIR}/static/index.html"
    echo "  Updated static/index.html"
fi

chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

# Restart service
systemctl restart "${APP_NAME}"
sleep 2

if systemctl is-active --quiet "${APP_NAME}"; then
    echo "Service restarted successfully"
else
    echo "WARNING: Service may not have started. Check: sudo journalctl -u ${APP_NAME} -n 20"
fi

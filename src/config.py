"""Configuration for the dashboard metrics collector."""

import os
from dotenv import load_dotenv

load_dotenv()

# Selector API
SELECTOR_API_URL = "https://alkira-prod.selector.ai/api/collab2-slack/copilot/v1/chat"
SELECTOR_MCP_URL = "https://alkira-prod.selector.ai/mcp/"
SELECTOR_API_KEY = os.environ.get("SELECTOR_AI_API_KEY", "m34D6tY0YJei1TxmTQpmTQzN")

# Paths
BASE_DIR = os.environ.get("DASHBOARD_BASE_DIR", "/opt/dashboard")
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
WEB_ROOT = "/var/www/dashboard"


# For local development
if os.environ.get("DASHBOARD_LOCAL"):
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(BASE_DIR, "data")
    OUTPUT_DIR = os.path.join(BASE_DIR, "output")
    WEB_ROOT = OUTPUT_DIR

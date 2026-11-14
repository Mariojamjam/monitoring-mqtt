"""``python -m monitoring_mqtt.dashboard``: serve the dashboard (Streamlit) on port 8501.

Settings: ``API_URL`` (the API to read from) and ``DASHBOARD_REFRESH_SECONDS`` (live
refresh; 0 turns it off). Never opens a browser, never sends usage statistics and never
looks up the machine's public address (Streamlit does unless ``browser.serverAddress`` is
set; the address only appears in its start-up log).
"""

from __future__ import annotations

import sys
from pathlib import Path

from streamlit.web import cli

APP = Path(__file__).with_name("app.py")
PORT = 8501


OPTIONS = {
    "server.address": "0.0.0.0",
    "server.port": str(PORT),
    "server.headless": "true",
    "browser.gatherUsageStats": "false",
    "browser.serverAddress": "localhost",
    "client.toolbarMode": "minimal",
}


def main() -> int:
    options = [arg for name, value in OPTIONS.items() for arg in (f"--{name}", value)]
    # Options go before the script: after it, Streamlit passes them to the script.
    sys.argv = ["streamlit", "run", *options, *sys.argv[1:], str(APP)]
    return cli.main()


if __name__ == "__main__":
    sys.exit(main())

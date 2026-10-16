"""Starts the dashboard AND the automatic-sync timer together, so the daily syncs happen even when nobody has the page open.

    On your computer:     python run.py
    On Railway:           set the service Start Command to   python run.py
"""
import os
import sys

os.environ["RMS_RUN_PY"] = "1"          # lets the admin panel know the timer is always running

import database                          # noqa: E402  (same module the dashboard uses)

database.load_env_file()                  # read the optional .env file first (settings and API login)
database.start_scheduler()               # background timer: checks every minute whether an automatic sync is due

args = ["streamlit", "run", "app.py"]
if os.environ.get("PORT"):               # on Railway: listen on the port it gives us
    args += ["--server.port", os.environ["PORT"], "--server.address", "0.0.0.0",
             "--server.headless", "true", "--browser.gatherUsageStats", "false"]
sys.argv = args

from streamlit.web import cli as stcli   # noqa: E402

sys.exit(stcli.main())

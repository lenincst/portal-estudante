"""Static, non-secret configuration (URLs, private data directory). No personal data or ids."""

from __future__ import annotations

import os
from pathlib import Path

KEYRING_SERVICE = "portal-estudante"

# Ulife
ULIFE_LOGIN_URL = "https://www.ulife.com.br/login.aspx"
ULIFE_STUDENT_HOME = "https://student.ulife.com.br/NewStudentHome"

# Private local data (outside the project folder on purpose: holds browser
# profile cookies, captures and SDK state). Windows per-user ACLs protect %LOCALAPPDATA%.
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "portal-estudante"

# Pacing is fixed in session.py (PACE_MIN/PACE_JITTER/BACKOFF_STEPS), intentionally not configurable.

# Optional request budget for verification runs (the library itself has no fixed caps; loops are detected).
LIVE_CHECK_REQUEST_BUDGET = 120

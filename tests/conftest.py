"""Test session guard: no test may write to the real house (the simulator reuses the real device ids)."""

import os
import tempfile
from pathlib import Path

os.environ["TT_NO_REAL_WRITES"] = "1"
# Tests that use get_settings() as-is must not read or write var/ (home_sim.db, and the user's 3D layout
# next to it, which would e.g. turn the contact sensor into a door sensor).
os.environ.setdefault("HISTORY_DB", str(Path(tempfile.mkdtemp(prefix="tt_tests_")) / "home_test.db"))

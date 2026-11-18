"""Test session guard: no test may write to the real house (the simulator reuses the real device ids)."""

import os

os.environ["TT_NO_REAL_WRITES"] = "1"

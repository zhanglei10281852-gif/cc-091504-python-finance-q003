from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from equity_platform import TrancheStatus  # noqa: E402
from helpers import dt, make_platform  # noqa: E402


class TimezoneCutoffTest(unittest.TestCase):
    """跨时区截止点：归属日按协议时区自然日界定，与执行地点无关。"""

    def test_not_vested_before_shanghai_midnight(self):
        platform = make_platform()
        # 上海 2025-01-14 00:00 对应 UTC 2025-01-13 16:00
        platform.recalculate("G1", dt(2025, 1, 13, 15, 59, 59))
        self.assertEqual(TrancheStatus.PENDING, platform.tranches["G1:1"].status)

    def test_vested_at_shanghai_midnight(self):
        platform = make_platform()
        platform.recalculate("G1", dt(2025, 1, 13, 16, 0, 0))
        self.assertEqual(TrancheStatus.RELEASABLE, platform.tranches["G1:1"].status)

    def test_same_instant_same_conclusion_regardless_of_input_tz(self):
        from datetime import timezone, timedelta

        platform = make_platform()
        instant_utc = dt(2025, 1, 13, 16, 0, 0)
        instant_elsewhere = instant_utc.astimezone(timezone(timedelta(hours=-5)))
        platform.recalculate("G1", instant_elsewhere)
        self.assertEqual(TrancheStatus.RELEASABLE, platform.tranches["G1:1"].status)


if __name__ == "__main__":
    unittest.main()

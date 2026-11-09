import os
import tempfile
import time
import unittest

from core import monitor
from core.config import Config
from core.index import Index
from core.scanner import DirRec
from core.util import GB
from core.volumes import Volume


class AlertRuleTests(unittest.TestCase):
    def test_low_space_by_percent(self):
        alert = monitor.low_space_alert("C:", 1000 * GB, 50 * GB, low_pct=10, low_gb=20)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.kind, "low")

    def test_low_space_by_absolute(self):
        self.assertIsNotNone(monitor.low_space_alert("C:", 100 * GB, 15 * GB, low_pct=10, low_gb=20))

    def test_enough_space(self):
        self.assertIsNone(monitor.low_space_alert("D:", 1000 * GB, 300 * GB, low_pct=10, low_gb=20))

    def test_drop_alert(self):
        now = 10_000
        history = [(now - 3000, 100 * GB), (now - 1000, 97 * GB), (now, 94 * GB)]
        alert = monitor.drop_alert("C:", history, now, 3600, 5 * GB)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.kind, "drop")

    def test_drop_outside_window_ignored(self):
        now = 10_000
        history = [(now - 7200, 100 * GB), (now - 100, 94 * GB), (now, 94 * GB)]
        self.assertIsNone(monitor.drop_alert("C:", history, now, 3600, 5 * GB))

    def test_scan_due(self):
        self.assertTrue(monitor.scan_due(0, 24, now=1000))
        self.assertFalse(monitor.scan_due(1000, 24, now=2000))
        self.assertTrue(monitor.scan_due(1000, 24, now=1000 + 25 * 3600))
        self.assertFalse(monitor.scan_due(0, 0, now=1000))

    def test_idle_seconds_non_negative(self):
        self.assertGreaterEqual(monitor.idle_seconds(), 0)


class SamplerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_mon_")
        self.index = Index(os.path.join(self._tmp.name, "index.db"))
        self.config = Config(os.path.join(self._tmp.name, "config.json"))

    def tearDown(self):
        self.index.close()
        self._tmp.cleanup()

    def test_sample_records_and_dedupes_alerts(self):
        sampler = monitor.Sampler(self.index, self.config)
        vols = [Volume("C:", "OS", "NTFS", 400 * GB, 10 * GB)]
        now = time.time()
        first = sampler.sample(vols, now=now)
        self.assertEqual([a.kind for a in first], ["low"])
        second = sampler.sample(vols, now=now + 600)
        self.assertEqual(second, [])
        self.assertEqual(len(self.index.volume_history("C:")), 2)

    def test_change_since(self):
        now = int(time.time())
        self.index.add_volume_sample("C:", 100, 50, now - 3000)
        self.index.add_volume_sample("C:", 100, 40, now)
        self.assertEqual(monitor.change_since(self.index, "C:", 3600, now), -10)
        self.assertIsNone(monitor.change_since(self.index, "D:", 3600, now))

    def test_growth_prefers_child(self):
        now = int(time.time())
        base = [
            DirRec("C:\\Users", "C:\\", "C:", 2, 100 * GB, 1, now),
            DirRec("C:\\Users\\me", "C:\\Users", "C:", 3, 90 * GB, 1, now),
            DirRec("C:\\Users\\me\\cache", "C:\\Users\\me", "C:", 4, 1 * GB, 1, now),
        ]
        self.index.replace_scan([], base, [], [], drives=["C:"])
        grown = [
            DirRec("C:\\Users", "C:\\", "C:", 2, 111 * GB, 1, now),
            DirRec("C:\\Users\\me", "C:\\Users", "C:", 3, 101 * GB, 1, now),
            DirRec("C:\\Users\\me\\cache", "C:\\Users\\me", "C:", 4, 11 * GB, 1, now),
        ]
        self.index.replace_scan([], grown, [], [], drives=["C:"])
        result = monitor.growth(self.index)
        paths = [r["path"] for r in result["rows"]]
        self.assertEqual(paths[0], "C:\\Users\\me\\cache")
        self.assertNotIn("C:\\Users\\me", paths)
        self.assertEqual(result["rows"][0]["delta"], 10 * GB)

    def test_growth_needs_two_scans(self):
        self.assertEqual(monitor.growth(self.index)["rows"], [])


class AutostartTests(unittest.TestCase):
    def test_command_mentions_app(self):
        cmd = monitor.autostart_command(r"C:\app")
        self.assertIn("--minimized", cmd)
        self.assertIn("app.py", cmd)


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import unittest

from core import config as config_mod
from core.config import Config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hdm_cfg_")
        self.path = os.path.join(self._tmp.name, "config.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_defaults(self):
        cfg = Config(self.path)
        self.assertEqual(cfg.scan["drives"], ["C:", "D:"])
        self.assertGreaterEqual(cfg.scan["min_big_size"], 100 * 1024 * 1024)
        self.assertFalse(cfg.ai_ready())

    def test_save_and_load(self):
        cfg = Config(self.path)
        cfg.ai["model"] = "my-model"
        cfg.api_key = "sk-test-1234567890"
        cfg.privacy["mask_user"] = False
        cfg.save()
        self.assertTrue(os.path.exists(self.path))
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["ai"]["model"], "my-model")
        self.assertNotIn("sk-test-1234567890", json.dumps(raw))

        cfg2 = Config(self.path)
        self.assertEqual(cfg2.ai["model"], "my-model")
        self.assertEqual(cfg2.api_key, "sk-test-1234567890")
        self.assertFalse(cfg2.privacy["mask_user"])

    def test_corrupt_file_falls_back(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        cfg = Config(self.path)
        self.assertEqual(cfg.data["version"], config_mod.CONFIG_VERSION)

    def test_v1_config_migrates_threads_to_auto(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "scan": {"threads": 4, "drives": ["C:"]}}, fh)
        cfg = Config(self.path)
        self.assertEqual(cfg.scan["threads"], 0)
        self.assertEqual(cfg.scan["drives"], ["C:"])
        self.assertIn("low_pct", cfg.monitor)

    def test_masked_key(self):
        cfg = Config(self.path)
        cfg.api_key = "sk-abcdefghijklmn"
        self.assertIn("*", cfg.masked_key())
        self.assertEqual(cfg.masked_key()[:4], "sk-a")

    def test_set_nested(self):
        cfg = Config(self.path)
        cfg.set("ai.temperature", 0.9)
        self.assertEqual(cfg.get("ai.temperature"), 0.9)
        self.assertIsNone(cfg.get("nope.nothing"))

    def test_invalid_temperature_accepted_but_clamped_elsewhere(self):
        cfg = Config(self.path)
        cfg.ai["temperature"] = "abc"
        cfg.save()
        cfg2 = Config(self.path)
        self.assertEqual(cfg2.ai["temperature"], "abc")

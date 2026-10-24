import importlib.util
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("ed2k_file_size_test", Path(__file__).resolve().parents[1] / "file_size.py")
sizes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sizes)


class FileSizeTests(unittest.TestCase):
    def test_gb_input_and_legacy_conversion_preserve_exact_threshold(self):
        for legacy in ("500M", "100M", "1B", "1024T", "0.001K", "1.5G", ""):
            with self.subTest(legacy=legacy):
                gb = sizes.minimum_gb_value(legacy)
                self.assertEqual(sizes.minimum_file_bytes(sizes.minimum_gb_setting(gb)), sizes.minimum_file_bytes(legacy))
        self.assertEqual(sizes.minimum_gb_value("500M"), "0.48828125")
        self.assertEqual(sizes.minimum_file_bytes(sizes.minimum_gb_setting("0.5")), 512 * 1024 ** 2)
        self.assertEqual(sizes.minimum_gb_setting(""), "")
        for invalid in (True, None, "500M", "-1", "NaN", "1e3", "1048577", "0." ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                sizes.minimum_gb_setting(invalid)

    def test_units_decimals_empty_and_exact_bytes(self):
        for value, expected in (("", 0), ("0", 0), ("100M", 100 * 1024 ** 2), ("1.5g", 1536 * 1024 ** 2), ("2 MiB", 2 * 1024 ** 2), ("5", 5), ("0.001K", 2), ("1024T", 1024 ** 5)):
            with self.subTest(value=value):
                self.assertEqual(sizes.minimum_file_bytes(value), expected)

    def test_invalid_and_excessive_values_rejected(self):
        for value in (None, True, 12, "-1M", "NaN", "Infinity", "1e6", "1BB", "1PB", "1025T", "1,000M", "9" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sizes.minimum_file_bytes(value)

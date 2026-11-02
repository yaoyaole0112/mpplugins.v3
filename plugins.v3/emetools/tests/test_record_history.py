import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from record_history import RECORD_TTL_SECONDS, recent_records


class RecordHistoryTests(unittest.TestCase):
    def test_keeps_only_records_from_the_last_24_hours(self):
        now = 2_000_000
        records = [
            {"timestamp": now - RECORD_TTL_SECONDS - 1, "name": "old"},
            {"timestamp": now - RECORD_TTL_SECONDS, "name": "boundary"},
            {"timestamp": now, "name": "current"},
        ]

        self.assertEqual(
            [item["name"] for item in recent_records(records, now=now)],
            ["boundary", "current"],
        )

    def test_legacy_records_receive_a_migration_timestamp(self):
        now = 2_000_000

        self.assertEqual(
            recent_records([{"name": "legacy"}], now=now),
            [{"name": "legacy", "timestamp": now}],
        )

    def test_limit_keeps_newest_entries(self):
        now = 2_000_000
        records = [{"timestamp": now - offset, "name": str(offset)} for offset in range(5)]

        self.assertEqual(
            [item["name"] for item in recent_records(records, now=now, limit=2)],
            ["0", "1"],
        )


if __name__ == "__main__":
    unittest.main()

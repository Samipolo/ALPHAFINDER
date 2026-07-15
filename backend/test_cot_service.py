import os
import sys
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(__file__))

from services import cot


class CotServiceTests(unittest.TestCase):
    def test_active_day_cache_ttl_on_tuesday_and_friday(self):
        tuesday_et = datetime(2026, 4, 28, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        friday_morning_et = datetime(2026, 5, 1, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        monday_et = datetime(2026, 4, 27, 10, 0, tzinfo=ZoneInfo("America/New_York"))

        self.assertEqual(
            cot._current_cache_ttl(tuesday_et.astimezone(timezone.utc)),
            cot.ACTIVE_DAY_CACHE_TTL,
        )
        # Friday morning (before publish window) → 4-min active TTL
        self.assertEqual(
            cot._current_cache_ttl(friday_morning_et.astimezone(timezone.utc)),
            cot.ACTIVE_DAY_CACHE_TTL,
        )
        self.assertEqual(
            cot._current_cache_ttl(monday_et.astimezone(timezone.utc)),
            cot.BASE_CACHE_TTL,
        )

    def test_publish_window_ttl_friday_afternoon(self):
        """Friday 3-6 PM ET uses ultra-aggressive 2-min TTL."""
        friday_330pm = datetime(2026, 5, 1, 15, 30, tzinfo=ZoneInfo("America/New_York"))
        friday_500pm = datetime(2026, 5, 1, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        friday_600pm = datetime(2026, 5, 1, 18, 0, tzinfo=ZoneInfo("America/New_York"))

        # Inside publish window → 2-min TTL
        self.assertEqual(
            cot._current_cache_ttl(friday_330pm.astimezone(timezone.utc)),
            cot.PUBLISH_WINDOW_TTL,
        )
        self.assertEqual(
            cot._current_cache_ttl(friday_500pm.astimezone(timezone.utc)),
            cot.PUBLISH_WINDOW_TTL,
        )
        # 6 PM is outside the window → falls back to 4-min active TTL
        self.assertEqual(
            cot._current_cache_ttl(friday_600pm.astimezone(timezone.utc)),
            cot.ACTIVE_DAY_CACHE_TTL,
        )

    def test_ttl_values_fit_under_5min_poll(self):
        """All TTL values must be under 5 minutes (300s) to ensure data freshness."""
        self.assertLessEqual(cot.BASE_CACHE_TTL, 300)
        self.assertLessEqual(cot.ACTIVE_DAY_CACHE_TTL, 300)
        self.assertLessEqual(cot.PUBLISH_WINDOW_TTL, 300)

    def test_is_publish_window(self):
        """Only Friday 3-6 PM ET qualifies."""
        fri_3pm = datetime(2026, 5, 1, 15, 0, tzinfo=ZoneInfo("America/New_York"))
        tue_3pm = datetime(2026, 4, 28, 15, 0, tzinfo=ZoneInfo("America/New_York"))

        self.assertTrue(cot._is_publish_window(fri_3pm))
        self.assertFalse(cot._is_publish_window(tue_3pm))

    def test_parse_cot_attaches_report_and_publish_metadata(self):
        raw = [{
            "market_and_exchange_names": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
            "report_date_as_yyyy_mm_dd": "2026-04-21T00:00:00.000",
            "noncomm_positions_long_all": "100",
            "noncomm_positions_short_all": "40",
            "open_interest_all": "200",
            "change_in_noncomm_long_all": "10",
            "change_in_noncomm_short_all": "-5",
        }]
        marker = {
            "report_date_as_yyyy_mm_dd": "2026-04-21T00:00:00.000",
            ":created_at": "2026-04-24T19:31:21.848Z",
            ":updated_at": "2026-04-24T19:31:21.848Z",
        }

        parsed = cot._parse_cot(raw, marker=marker)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["symbol"], "EUR")
        self.assertEqual(parsed[0]["report_date"], "2026-04-21")
        self.assertEqual(parsed[0]["source_published_at"], "2026-04-24T19:31:21.848Z")
        self.assertEqual(parsed[0]["source_updated_at"], "2026-04-24T19:31:21.848Z")

    def test_apply_marker_to_rows_enriches_cached_rows(self):
        rows = [{"symbol": "USD", "report_date": "2026-04-21"}]
        marker = {
            "report_date_as_yyyy_mm_dd": "2026-04-21T00:00:00.000",
            ":created_at": "2026-04-24T19:31:21.848Z",
            ":updated_at": "2026-04-24T19:31:21.848Z",
        }

        enriched = cot._apply_marker_to_rows(rows, marker)

        self.assertEqual(enriched[0]["report_date"], "2026-04-21")
        self.assertEqual(enriched[0]["source_published_at"], "2026-04-24T19:31:21.848Z")
        self.assertEqual(enriched[0]["source_updated_at"], "2026-04-24T19:31:21.848Z")


if __name__ == "__main__":
    unittest.main()

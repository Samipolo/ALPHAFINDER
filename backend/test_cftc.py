import unittest


def treasury_name_candidates(rows):
    names = {row.get("market_and_exchange_names", "") for row in rows if isinstance(row, dict)}
    return sorted(
        name for name in names
        if any(token in name.upper() for token in ("10-YEAR U", "10 YEAR U", "TREASURY NOTE"))
    )


class TestCftcNameFilter(unittest.TestCase):
    def test_filters_treasury_note_variants(self):
        rows = [
            {"market_and_exchange_names": "U.S. TREASURY NOTE - CHICAGO BOARD OF TRADE"},
            {"market_and_exchange_names": "EURO FX - CHICAGO MERCANTILE EXCHANGE"},
            {"market_and_exchange_names": "10-Year U.S. Treasury Note"},
        ]
        filtered = treasury_name_candidates(rows)
        self.assertEqual(len(filtered), 2)
        self.assertIn("10-Year U.S. Treasury Note", filtered)
        self.assertIn("U.S. TREASURY NOTE - CHICAGO BOARD OF TRADE", filtered)


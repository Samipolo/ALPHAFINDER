import unittest


def unique_market_names(rows):
    return sorted({row.get("market_and_exchange_names", "") for row in rows if isinstance(row, dict)})


class TestCftcNameExtraction(unittest.TestCase):
    def test_returns_unique_sorted_market_names(self):
        rows = [
            {"market_and_exchange_names": "EURO FX - CHICAGO MERCANTILE EXCHANGE"},
            {"market_and_exchange_names": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE"},
            {"market_and_exchange_names": "EURO FX - CHICAGO MERCANTILE EXCHANGE"},
        ]
        names = unique_market_names(rows)
        self.assertEqual(
            names,
            [
                "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
                "EURO FX - CHICAGO MERCANTILE EXCHANGE",
            ],
        )


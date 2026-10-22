from __future__ import annotations

import unittest
from datetime import datetime, timezone

from goldwatch.quote import (
    TROY_OUNCE_GRAMS,
    Quote,
    convert,
    format_price,
    needs_fx,
    reference_line,
)


class QuoteConversionTest(unittest.TestCase):
    def test_usd_oz_to_cny_g(self) -> None:
        usdcny = 6.676
        converted = convert(4118.5, "USD/oz", "CNY/g", usdcny)
        self.assertAlmostEqual(converted, 4118.5 * usdcny / TROY_OUNCE_GRAMS, places=6)

    def test_round_trip(self) -> None:
        usdcny = 7.12
        cny_g = convert(4200.0, "USD/oz", "CNY/g", usdcny)
        back = convert(cny_g, "CNY/g", "USD/oz", usdcny)
        self.assertAlmostEqual(back, 4200.0, places=6)

    def test_same_unit_does_not_need_fx(self) -> None:
        self.assertEqual(convert(900.0, "CNY/g", "CNY/g", None), 900.0)
        self.assertFalse(needs_fx("CNY/g", "CNY/oz"))
        self.assertTrue(needs_fx("USD/oz", "CNY/g"))

    def test_cny_g_to_cny_oz_needs_no_fx(self) -> None:
        self.assertAlmostEqual(convert(900.0, "CNY/g", "CNY/oz", None), 900.0 * TROY_OUNCE_GRAMS, places=6)
        self.assertAlmostEqual(convert(900.0 * TROY_OUNCE_GRAMS, "CNY/oz", "CNY/g", None), 900.0, places=6)

    def test_conversion_requires_fx(self) -> None:
        with self.assertRaises(Exception):
            convert(4000.0, "USD/oz", "CNY/g", None)

    def test_format_price(self) -> None:
        self.assertEqual(format_price(885.4321, "CNY/g"), "885.432 CNY/g")
        self.assertEqual(format_price(4118.5, "USD/oz"), "4,118.50 USD/oz")

    def test_reference_line(self) -> None:
        quote = Quote(
            price=4118.5,
            unit="USD/oz",
            source="test",
            ts=datetime.now(timezone.utc),
        )
        line = reference_line(quote, 6.676)
        self.assertIsNotNone(line)
        assert line is not None
        self.assertIn("CNY/g", line)


if __name__ == "__main__":
    unittest.main()

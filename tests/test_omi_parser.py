"""Testy parsera specyfikacji OMI XTB (dane syntetyczne w formacie dokumentu; nie wymaga pliku PDF)."""
import os, sys, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from invapp.brokers.xtb import omi_parser as p  # noqa: E402

HEADER = "Specification Table Organised Market Instruments (OMI)\nand\nSpecification Table Fractional Rights\n"


def pages(*texts):
    return [HEADER] + list(texts)


class TestOmiParser(unittest.TestCase):
    def test_parses_stock_and_etf_rows_without_guessing_exchange_or_unit(self):
        rows, unp, st = p.parse_pages(pages(
            "Stocks\nSymbol Company Name ISIN Currency Minimum transaction value Trading Hours Trading Days\n"
            "NVDA.US NVIDIA Corp US67066G1040 USD 1 USD 15:30 - 22:00 Monday - Friday\n"
            "ETF, ETN, ETC\n"
            "VUAA.UK Vanguard S&P 500 UCITS ETF (Acc USD) IE00BFMXXD54 USD ETF 1 USD 09:00 - 17:30 Monday - Friday\n"
            "VUAG.UK Vanguard S&P 500 UCITS ETF (Acc GBP) IE00BFMXXD54 GBP ETF 1 GBP 09:00 - 17:30 Monday - Friday\n"))
        self.assertEqual(unp, [])
        by = {r["symbol"]: r for r in rows}
        self.assertEqual((by["NVDA.US"]["type"], by["NVDA.US"]["currency"]), ("STOCK", "USD"))
        self.assertEqual((by["VUAA.UK"]["currency"], by["VUAG.UK"]["currency"]), ("USD", "GBP"))     # ten sam ISIN, rozne waluty linii
        self.assertEqual(by["VUAA.UK"]["isin"], by["VUAG.UK"]["isin"])
        self.assertNotIn("mic", by["VUAA.UK"]); self.assertNotIn("price_unit", by["VUAA.UK"])         # dokument nie podaje gieldy ani jednostki ceny

    def test_suffix_is_not_used_to_derive_currency(self):
        rows, _, _ = p.parse_pages(pages(
            "ABC.UK Alpha Plc GB0000000001 GBP 1 GBP 09:00 - 17:30 Monday - Friday\n"
            "DEF.UK Delta ETF (Acc USD) IE0000000002 USD ETF 1 USD 09:00 - 17:30 Monday - Friday\n"
            "GHI.UK Gamma ETF (Acc EUR) IE0000000003 EUR ETF 1 EUR 09:00 - 17:30 Monday - Friday\n"))
        self.assertEqual({r["symbol"]: r["currency"] for r in rows}, {"ABC.UK": "GBP", "DEF.UK": "USD", "GHI.UK": "EUR"})

    def test_unparsable_symbol_lines_are_reported_not_skipped(self):
        rows, unp, st = p.parse_pages(pages("BAD.US Cos dziwnego bez isin i waluty\nOK.US Ok Inc US0000000009 USD 1 USD 15:30 - 22:00 Monday - Friday\n"))
        self.assertEqual(len(rows), 1); self.assertEqual(len(unp), 1); self.assertEqual(st["unparsed"], 1)

    def test_conflicting_symbol_is_excluded_and_reported(self):
        rows, unp, st = p.parse_pages(pages(
            "LB.US* CLOSE ONLY / LandBridge Co LLC US5149521008 USD 1 USD 15:30 - 22:00 Monday - Friday\n"
            "LB.US* CLOSE ONLY / L Brands Inc US0708301041 USD 1 USD 15:30 - 22:00 Monday - Friday\n"
            "AAA.US Aaa Inc US0000000001 USD 1 USD 15:30 - 22:00 Monday - Friday\n"))
        self.assertEqual([r["symbol"] for r in rows], ["AAA.US"]); self.assertEqual(st["conflicting_symbols"], ["LB.US"])

    def test_identical_duplicates_are_harmless_and_fractional_rights_table_is_ignored(self):
        rows, _, st = p.parse_pages(pages(
            "AAA.US Aaa Inc US0000000001 USD 1 USD 15:30 - 22:00 Monday - Friday\n"
            "AAA.US Aaa Inc US0000000001 USD 1 USD 15:30 - 22:00 Monday - Friday\n"
            "Specification Table Fractional Rights\n"
            "AAA.US Aaa Inc US0000000001 USD 1 USD 0.0001 15:30 - 22:00 Monday - Friday\n"
            "ZZZ.US Zzz Inc US0000000002 USD 1 USD 0.0001 15:30 - 22:00 Monday - Friday\n"))
        self.assertEqual([r["symbol"] for r in rows], ["AAA.US"]); self.assertEqual(st["conflicting_symbols"], [])

    def test_rejects_foreign_document(self):
        with self.assertRaises(ValueError):
            p.parse_pages(["Inny dokument\n"])

    def test_close_only_marker_is_kept_not_part_of_symbol(self):
        rows, _, _ = p.parse_pages(pages("888.UK* CLOSE ONLY / 888 Holdings PLC GI000A0F6407 GBP 1 GBP 09:00 - 17:30 Monday - Friday\n"))
        self.assertEqual((rows[0]["symbol"], rows[0]["close_only"], rows[0]["currency"]), ("888.UK", True, "GBP"))


if __name__ == "__main__":
    unittest.main()

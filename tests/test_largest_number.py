from decimal import Decimal
from pathlib import Path
import shutil
import unittest

from largest_number import best_candidates, extract_candidates

try:
    import pytesseract  # noqa: F401
except ImportError:
    pytesseract = None


class IntegrationTests(unittest.TestCase):
    air_force_pdf = Path("FY25_Air_Force_Working_Capital_Fund.pdf")
    army_pdf = Path("Army_Working_Capital_Fund.pdf")
    scanned_pdf = Path("Army_Navy_Working_Capital_Transfer.pdf")

    @unittest.skipUnless(air_force_pdf.exists(), "FY25 Air Force PDF fixture is not present")
    def test_air_force_assignment_document(self):
        raw, adjusted = best_candidates(extract_candidates(self.air_force_pdf))

        self.assertEqual(raw.page, 93)
        self.assertEqual(raw.raw_value, Decimal("6000000"))
        self.assertEqual(raw.token, "$6,000,000)")

        self.assertEqual(adjusted.page, 13)
        self.assertEqual(adjusted.raw_value, Decimal("30704.1"))
        self.assertEqual(adjusted.adjusted_value, Decimal("30704100000.0"))
        self.assertIn("AFWCF Financial Summary", adjusted.verification_path)
        self.assertIn("row: Total Revenue", adjusted.verification_path)
        self.assertIn("column: FY 2025", adjusted.verification_path)

    @unittest.skipUnless(army_pdf.exists(), "Army PDF fixture is not present")
    def test_army_document_chart_axis_unit(self):
        raw, adjusted = best_candidates(extract_candidates(self.army_pdf))

        self.assertEqual(raw.page, 36)
        self.assertEqual(raw.raw_value, Decimal("705883"))

        self.assertEqual(adjusted.page, 32)
        self.assertEqual(adjusted.raw_value, Decimal("20000.0"))
        self.assertEqual(adjusted.adjusted_value, Decimal("20000000000.0"))
        self.assertIn("Chart SM 5", adjusted.verification_path)
        self.assertIn("scale label: 20,000.0", adjusted.verification_path)

    @unittest.skipUnless(
        scanned_pdf.exists() and pytesseract is not None and shutil.which("tesseract"),
        "Scanned PDF fixture or OCR engine is not present",
    )
    def test_scanned_document_ocr_fallback(self):
        raw, adjusted = best_candidates(extract_candidates(self.scanned_pdf))

        self.assertEqual(raw.page, 1)
        self.assertEqual(raw.raw_value, Decimal("1696323"))

        self.assertEqual(adjusted.page, 1)
        self.assertEqual(adjusted.adjusted_value, Decimal("1696323000"))
        self.assertEqual(adjusted.unit, "(Amounts in Thousands of Dollars)")


if __name__ == "__main__":
    unittest.main()

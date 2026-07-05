# Largest Number in a PDF

This project finds:

1. The largest raw number printed in a PDF.
2. The largest adjusted value after applying nearby unit guidance such as `(Dollars in Millions)`, `(Dollars in Thousands)`, `Amounts in Thousands of Dollars`, `$246K`, or `$9.6 billion`.

The solution is fully local and does not call external APIs.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For scanned/image-only PDFs, install the local Tesseract OCR engine too:

```bash
brew install tesseract
```

`pytesseract` is included in `requirements.txt`, but the Tesseract binary must be installed separately.

## Run

```bash
python largest_number.py FY25_Air_Force_Working_Capital_Fund.pdf
```

JSON output:

```bash
python largest_number.py FY25_Air_Force_Working_Capital_Fund.pdf --json
```

Tests:

```bash
python -m unittest discover -s tests
```

## Design

The scanner reads the PDF page by page with `pdfplumber`. If a page has no embedded text but contains images, it falls back to local Tesseract OCR. Each number candidate keeps an audit trail: raw token, raw value, adjusted value, page, verification path, context line, unit, adjustment reason, and classification.

The parser uses exact `Decimal` arithmetic instead of floats, so large numbers and decimal values are compared without floating-point rounding issues.

## Handled Cases

- comma and decimal numbers: `30,704.1`
- leading decimals: `.309`
- parenthesized negatives: `(46.6)`
- explicit full currency: `$6,000,000`
- suffix scales: `$5M`, `$246K`, `$9.6 billion`
- table/page units: `(Dollars in Millions)`, `($ in Millions)`, `Amounts in Thousands of Dollars`
- chart-axis units, including vertical `$ Millions`
- text PDFs, image-only PDFs with OCR installed, and mixed PDFs at the page level
- non-financial counts/rates that should not inherit dollar units

## Scale and Next Steps

The implementation is linear by page and only OCRs image-only pages, which keeps normal large text PDFs fast. For production use, the next improvement would be optional OCR for pages that contain both embedded text and screenshot tables, with deduplication to avoid counting the same number twice.

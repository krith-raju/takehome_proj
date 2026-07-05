# Largest Number in a PDF

This is a local Python solution for the ConductorAI take-home. Given a PDF, it reports:

1. The largest raw number printed in the document, regardless of unit.
2. The largest adjusted number after applying natural language unit guidance from the document.

The solution is self-contained.

Assumption: Negative values, including parenthesized values like `(46.6)`, are compared as signed numbers. The program looks for the greatest value, not the greatest absolute magnitude.

## Setup From a Fresh Clone

```bash
git clone https://github.com/krith-raju/takehome_proj.git
brew install tesseract
cd takehome_proj
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The virtual environment is created inside `takehome_proj` as `.venv/`. It is ignored by git.

`requirements.txt` installs:

- `pdfplumber`: extracts embedded PDF text page by page.
- `pypdfium2`: renders image-only PDF pages so OCR can read them.
- `pytesseract`: runs local Tesseract OCR from Python for scanned/image-only pages.

## Run

Place the PDF you want to scan in the repo directory, or pass an absolute/relative path to it:

```bash
python largest_number.py INSERT_PDF_HERE.pdf
```

Examples used while testing:

```bash
python largest_number.py FY25_Air_Force_Working_Capital_Fund.pdf
python largest_number.py FY27_Air_Force_Working_Capital_Fund.pdf
python largest_number.py Army_Working_Capital_Fund.pdf
python largest_number.py Army_Navy_Working_Capital_Transfer.pdf
```

JSON output:

```bash
python largest_number.py FY25_Air_Force_Working_Capital_Fund.pdf --json
```

## Output Fields

The program prints `Largest raw` and `Largest adjusted`.

- `value`: the winning value for that section.
- `token` / `raw token`: the exact number text found in the PDF.
- `raw value`: the parsed value before unit scaling, shown for adjusted results.
- `page`: the PDF page where the number was found.
- `unit`: detected unit guidance, if any.
- `adjustment`: why the value was or was not scaled.
- `classification`: parser category such as `financial`, `money`, `count_or_rate`, or `scale_label`.
- `path`: best-effort verification path using page title, table/chart title, row, and column when recoverable.
- `context`: the source line used as audit evidence.

Example:

```text
Largest adjusted
  value: 30,704,100,000
  raw token: 30,704.1
  raw value: 30,704.1
  page: 13
  unit: (Dollars in Millions)
  adjustment: (Dollars in Millions) from page
  classification: financial
  path: Page 13 > AFWCF Financial Summary > row: Total Revenue > column: FY 2025
  context: Total Revenue Total Revenue 28,239.2 29,176.6 30,704.1
```

## How the Code Works

1. Open the PDF with `pdfplumber`.
2. Extract embedded text from each page.
3. If a page has no text but contains images, render that page with `pypdfium2` and OCR it with local Tesseract.
4. Detect page/table unit guidance such as `(Dollars in Millions)`, `($ in Millions)`, or `Amounts in Thousands of Dollars`.
5. Extract numeric tokens with regex, including commas, decimals, currency signs, percentages, and parenthesized negatives.
6. Parse values with `Decimal` instead of floats to avoid rounding issues and support very large numbers.
7. Classify each number using nearby text so dollar units are not blindly applied to counts, percentages, IDs, manpower rows, or rates.
8. Apply scaling when the unit evidence is strong enough.
9. Keep an evidence record for every candidate and return the max raw and max adjusted values.

## Handled Cases

- `30,704.1`, `1,738.10`, `.309`
- `(46.6)` as a negative value
- `$6,000,000` as an explicit full currency amount
- `$5M`, `$246K`, `$9.6 billion`
- `(Dollars in Millions)`, `($ in Millions)`, `(Dollars in Thousands)`
- `Amounts in Thousands of Dollars`
- chart-axis labels such as vertical `$ Millions`
- text PDFs, image-only PDFs, and mixed PDFs at the page level
- financial rows versus non-financial counts/rates

## Tests

```bash
python -m unittest discover -s tests
```

The tests are integration-focused and run against real PDFs when those files are present locally. They verify:

- the FY25 Air Force assignment document result,
- the Army chart-axis unit case,
- the OCR fallback for a scanned/image-only document.

PDFs are intentionally ignored by git, so tests skip gracefully if a fixture is not present.

## Efficiency and Scale

The scanner is linear by page. It reads one page at a time, extracts candidates from text lines, and keeps only candidate records needed for comparison and audit output. OCR is only used for image-only pages, which keeps large text-based PDFs fast. For large scanned PDFs, the OCR renderer is opened once and reused across pages.

## Next Steps

The main production improvement would be optional OCR for pages that contain both embedded text and screenshot tables. That would require deduplication so the same visible number is not counted twice from both the text layer and OCR. Along with that, integrating this within a agentic workflow in which this would be used as a document parser to extract relevant values. 

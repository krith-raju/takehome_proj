#!/usr/bin/env python3
"""Find the largest raw and context-adjusted number in a PDF."""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

try:
    import pdfplumber
except ImportError:  # pragma: no cover - exercised only in a missing-dependency environment
    pdfplumber = None

try:
    import pypdfium2 as pdfium
except ImportError:  # pragma: no cover - exercised only in a missing-dependency environment
    pdfium = None

try:
    import pytesseract
except ImportError:  # pragma: no cover - exercised only in a missing-dependency environment
    pytesseract = None


logging.getLogger("pdfminer").setLevel(logging.ERROR)


SCALE_MULTIPLIERS = {
    "thousand": Decimal("1000"),
    "million": Decimal("1000000"),
    "billion": Decimal("1000000000"),
    "trillion": Decimal("1000000000000"),
}

LETTER_SCALES = {
    "k": "thousand",
    "m": "million",
    "b": "billion",
    "t": "trillion",
}

UNIT_PATTERNS = [
    re.compile(
        r"\(\s*(?P<category>dollars?|hours?)\s+in\s+"
        r"(?P<scale>thousands?|millions?|billions?|trillions?)\s*\)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\(\s*\$\s*(?:in\s+)?"
        r"(?P<scale>thousands?|millions?|billions?|trillions?|[kmbt])\s*\)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\(?\s*amounts?\s+in\s+"
        r"(?P<scale>thousands?|millions?|billions?|trillions?)\s+of\s+"
        r"(?P<category>dollars?|hours?)\s*\)?",
        re.IGNORECASE,
    ),
]

MONEY_SUFFIX_RE = re.compile(
    r"(?P<token>\$[\s]*[-+]?\(?\s*(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+|\.\d+)"
    r"(?:\.\d+)?)\s*\)?\s*(?P<scale>trillion|billion|million|thousand|[kmbt])\b)",
    re.IGNORECASE,
)

NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9-])"
    r"(?P<token>\(?\s*\$?\s*[-+]?(?:\d{1,3}(?:,\d{3})+|\d+|\.\d+)"
    r"(?:\.\d+)?%?\)?)"
    r"(?![A-Za-z0-9])"
)

FINANCIAL_CONTEXT_RE = re.compile(
    r"\b("
    r"revenue|cost|cash|disbursement|collection|sales|orders?|obligations?|"
    r"budget|appropriation|expenses?|income|inventory|investment|funding|"
    r"procurement|capital|maintenance|construction|milcon|rdt&e|wrm|"
    r"transfers?|reserves?|limit cash|operating result|target|price changes?"
    r")\b",
    re.IGNORECASE,
)

NON_FINANCIAL_CONTEXT_RE = re.compile(
    r"\b("
    r"end strength|workyears?|full time equivalents?|manpower|number of|"
    r"items managed|requisitions received|issues completed|procurement receipts|"
    r"contracts awarded|contracts executed|purchase inflation|rate per hour|"
    r"rate change|quality defect rate|due date performance|percent|barrels?|"
    r"per barrel|quantity|collection cycles?|standard deviations?|deviation"
    r")\b",
    re.IGNORECASE,
)

TABLE_HEADER_RE = re.compile(
    r"\b(fy20\d{2}|quantity|unit cost|total cost|element of cost|goal)\b",
    re.IGNORECASE,
)

FY_LABEL_RE = re.compile(r"\bFY\s*20\d{2}\b", re.IGNORECASE)
GENERIC_TITLE_RE = re.compile(
    r"\b("
    r"working capital fund|fiscal year|budget estimates|president's budget|"
    r"exhibit|unclassified"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class UnitContext:
    category: str
    scale: str
    multiplier: Decimal
    source_text: str
    source: str


@dataclass(frozen=True)
class NumericCandidate:
    page: int
    line_number: int
    token: str
    raw_value: Decimal
    adjusted_value: Decimal
    context: str
    unit: Optional[str]
    adjustment: str
    classification: str
    verification_path: Optional[str]

    def to_jsonable(self) -> dict:
        data = asdict(self)
        data["raw_value"] = str(self.raw_value)
        data["adjusted_value"] = str(self.adjusted_value)
        return data


def normalize_scale(scale: str) -> str:
    clean = scale.lower().strip().rstrip("s")
    return LETTER_SCALES.get(clean, clean)


def normalize_category(category: Optional[str]) -> str:
    if not category:
        return "dollars"
    clean = category.lower().strip().rstrip("s")
    if clean in {"$", "dollar"}:
        return "dollars"
    if clean == "hour":
        return "hours"
    return clean


def find_unit_contexts(line: str, source: str = "line") -> List[UnitContext]:
    """Find local unit guidance such as '(Dollars in Millions)'."""
    contexts: List[UnitContext] = []
    for pattern in UNIT_PATTERNS:
        for match in pattern.finditer(line):
            scale = normalize_scale(match.group("scale"))
            category = normalize_category(match.groupdict().get("category"))
            multiplier = SCALE_MULTIPLIERS.get(scale)
            if multiplier is None:
                continue
            contexts.append(
                UnitContext(
                    category=category,
                    scale=scale,
                    multiplier=multiplier,
                    source_text=match.group(0),
                    source=source,
                )
            )
    return contexts


def find_vertical_axis_unit(lines: Sequence[str]) -> Optional[UnitContext]:
    """Handle chart y-axis labels that extract as '$' and reversed 'Millions'."""
    joined = "\n".join(line.strip().lower() for line in lines)
    if "$" in joined and "snoillim" in joined:
        return UnitContext("dollars", "million", SCALE_MULTIPLIERS["million"], "$ Millions", "page")
    return None


def choose_page_unit(lines: Sequence[str]) -> Optional[UnitContext]:
    """Choose the most likely unit that applies to a page or table."""
    for line in lines[:12]:
        for context in find_unit_contexts(line, source="page"):
            return context
    axis_unit = find_vertical_axis_unit(lines)
    if axis_unit:
        return axis_unit
    for line in lines[12:]:
        for context in find_unit_contexts(line, source="page"):
            return context
    return None


def parse_decimal_token(token: str, line: str = "") -> Optional[Decimal]:
    """Parse a number exactly, preserving decimal precision and signed values."""
    stripped = token.strip()
    if not stripped:
        return None

    negative = False
    if stripped.startswith("(") and stripped.endswith(")") and "$" not in stripped:
        negative = True

    cleaned = stripped.strip()
    cleaned = cleaned.strip("()")
    cleaned = cleaned.replace("$", "")
    cleaned = cleaned.replace(",", "")
    cleaned = cleaned.replace("%", "")
    cleaned = cleaned.replace(" ", "")
    cleaned = cleaned.rstrip(".")

    if cleaned in {"", "+", "-"}:
        return None

    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None

    if negative:
        return -value
    return value


def is_unmatched_parenthetical_fragment(token: str) -> bool:
    stripped = token.strip()
    return stripped.endswith(")") and not stripped.startswith("(") and "$" not in stripped


def format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return f"{int(normalized):,}"
    plain = format(normalized, "f")
    if "." in plain:
        whole, frac = plain.split(".", 1)
        return f"{int(whole):,}.{frac.rstrip('0')}"
    return plain


def overlaps(span: Tuple[int, int], spans: Iterable[Tuple[int, int]]) -> bool:
    start, end = span
    return any(start < other_end and end > other_start for other_start, other_end in spans)


def number_matches(line: str) -> List[re.Match[str]]:
    return [match for match in NUMBER_RE.finditer(line)]


def clean_label(label: str) -> str:
    label = re.sub(r"\s+", " ", label).strip(" :-")
    if not label:
        return ""

    parts = label.split()
    if len(parts) % 2 == 0 and parts[: len(parts) // 2] == parts[len(parts) // 2 :]:
        label = " ".join(parts[: len(parts) // 2])
    return label


def row_label(line: str) -> Optional[str]:
    matches = number_matches(line)
    if not matches:
        return None
    label = clean_label(line[: matches[0].start()])
    return label or None


def is_title_like(line: str) -> bool:
    stripped = line.strip()
    if not stripped or find_unit_contexts(stripped):
        return False
    if stripped.endswith("."):
        return False
    if len(stripped) > 90:
        return False
    if len(stripped) > 50 and stripped.endswith("."):
        return False
    if NUMBER_RE.search(stripped):
        return False
    if len(stripped) < 4:
        return False
    if GENERIC_TITLE_RE.search(stripped):
        return False
    return any(char.isalpha() for char in stripped)


def find_title(lines: Sequence[str], line_number: int, classification: str) -> Optional[str]:
    if classification == "scale_label":
        for line in lines:
            if re.search(r"\bchart\b", line, re.IGNORECASE):
                return clean_label(line)

    before = list(enumerate(lines[:line_number], start=1))
    for _, line in reversed(before):
        if re.search(r"\b(table|chart)\b", line, re.IGNORECASE):
            return clean_label(line)

    for _, line in reversed(before):
        if is_title_like(line):
            return clean_label(line)

    for line in lines:
        if is_title_like(line):
            return clean_label(line)
    return None


def column_labels_from_header(line: str) -> List[str]:
    fy_labels = [match.group(0).upper().replace("  ", " ") for match in FY_LABEL_RE.finditer(line)]
    if len(fy_labels) >= 2:
        return fy_labels

    lowered = line.lower()
    if "total" in lowered and "demand based" in lowered and "mobilization based" in lowered:
        return ["FY 2024 TOTAL", "Demand Based", "Mobilization Based", "Non-Demand Based"]

    labels = []
    for label in ("Quantity", "Unit Cost", "Total Cost", "Amount"):
        labels.extend(re.findall(label, line, re.IGNORECASE))
    return labels


def find_column_label(lines: Sequence[str], line_number: int, value_index: int) -> Optional[str]:
    for header in reversed(lines[max(0, line_number - 8) : line_number]):
        labels = column_labels_from_header(header)
        if len(labels) > value_index:
            return clean_label(labels[value_index])
    return None


def build_verification_path(
    page_number: int,
    line_number: int,
    line: str,
    token: str,
    token_span: Tuple[int, int],
    lines: Sequence[str],
    classification: str,
) -> str:
    """Create a readable source path from nearby page, title, row, and column text."""
    segments = [f"Page {page_number}"]
    title = find_title(lines, line_number, classification)
    if title:
        segments.append(title)

    if classification == "scale_label":
        segments.append(f"scale label: {token.strip()}")
        return " > ".join(segments)

    label = row_label(line)
    if label:
        segments.append(f"row: {label}")
        matches = number_matches(line)
        value_index = sum(1 for match in matches if match.start() < token_span[0])
        column = find_column_label(lines, line_number - 1, value_index)
        if column:
            segments.append(f"column: {column}")
        elif len(matches) > 1:
            segments.append(f"value position: {value_index + 1}")

    return " > ".join(segments)


def is_explicit_full_currency(token: str, raw_value: Decimal) -> bool:
    clean = token.strip()
    if "$" not in clean or "." in clean:
        return False
    return "," in clean and abs(raw_value) >= Decimal("100000")


def is_identifier_like(token: str, line: str) -> bool:
    clean = token.strip().strip("()")
    no_punct = clean.replace("$", "").replace(",", "").replace(".", "")
    if not no_punct.isdigit():
        return False

    lowered = line.lower()
    if len(no_punct) >= 4 and re.search(r"\b(fund|appropriation|program element|project|milcon|rdt&e)\b", lowered):
        if "," not in clean and "." not in clean and "$" not in clean:
            return True
    if re.search(r"\bfy\s*20\d{2}\b", lowered) and no_punct.startswith("20"):
        return True
    return False


def is_table_header(line: str) -> bool:
    return bool(TABLE_HEADER_RE.search(line)) and not FINANCIAL_CONTEXT_RE.search(line)


def is_bare_scale_label(line: str, token: str, raw_value: Decimal, unit: Optional[UnitContext]) -> bool:
    return (
        unit is not None
        and line.strip() == token.strip()
        and abs(raw_value) >= Decimal("1000")
        and ("," in token or "." in token)
    )


def classify_context(line: str, token: str, raw_value: Decimal, unit: Optional[UnitContext]) -> str:
    """Classify a number so units are applied only when context supports it."""
    lowered = line.lower()
    if "%" in token or " percent" in lowered:
        return "percentage"
    if is_identifier_like(token, line):
        return "identifier"
    if is_bare_scale_label(line, token, raw_value, unit):
        return "scale_label"
    if is_table_header(line):
        return "table_header"
    if unit and unit.category != "dollars":
        return f"{unit.category}_value"
    if NON_FINANCIAL_CONTEXT_RE.search(line):
        return "count_or_rate"
    if FINANCIAL_CONTEXT_RE.search(line):
        return "financial"
    if "$" in token:
        return "money"
    return "number"


def adjust_candidate(
    raw_value: Decimal,
    token: str,
    line: str,
    unit: Optional[UnitContext],
    suffix_scale: Optional[str] = None,
) -> Tuple[Decimal, str, str]:
    """Apply explicit suffixes or nearby unit guidance to one numeric value."""
    if suffix_scale:
        scale = normalize_scale(suffix_scale)
        multiplier = SCALE_MULTIPLIERS[scale]
        return raw_value * multiplier, f"explicit {scale} suffix", "money"

    classification = classify_context(line, token, raw_value, unit)

    if is_explicit_full_currency(token, raw_value):
        return raw_value, "explicit full currency amount", classification

    if unit is None:
        return raw_value, "no unit adjustment", classification

    if classification in {"percentage", "identifier", "table_header", "count_or_rate"}:
        return raw_value, f"{classification}; unit not applied", classification

    if unit.category != "dollars":
        return raw_value * unit.multiplier, f"{unit.source_text} from {unit.source}", classification

    if classification in {"financial", "money", "scale_label"}:
        return raw_value * unit.multiplier, f"{unit.source_text} from {unit.source}", classification

    if classification == "number" and unit.category == "dollars" and "amount" in unit.source_text.lower():
        return raw_value * unit.multiplier, f"{unit.source_text} from {unit.source}", classification

    return raw_value, "unit context not strong enough", classification


def extract_line_candidates(
    page_number: int,
    line_number: int,
    line: str,
    page_unit: Optional[UnitContext],
    page_lines: Optional[Sequence[str]] = None,
) -> List[NumericCandidate]:
    """Extract all numeric candidates from one text line."""
    candidates: List[NumericCandidate] = []
    all_lines = page_lines or [line]
    local_units = find_unit_contexts(line, source="line")
    unit = local_units[0] if local_units else page_unit

    suffix_spans: List[Tuple[int, int]] = []
    for match in MONEY_SUFFIX_RE.finditer(line):
        token = match.group("token")
        value = parse_decimal_token(match.group("number"), line)
        if value is None:
            continue
        suffix_spans.append(match.span())
        adjusted, adjustment, classification = adjust_candidate(
            value,
            token,
            line,
            unit,
            suffix_scale=match.group("scale"),
        )
        candidates.append(
            NumericCandidate(
                page=page_number,
                line_number=line_number,
                token=token.strip(),
                raw_value=value,
                adjusted_value=adjusted,
                context=line.strip(),
                unit=None,
                adjustment=adjustment,
                classification=classification,
                verification_path=build_verification_path(
                    page_number,
                    line_number,
                    line,
                    token,
                    match.span(),
                    all_lines,
                    classification,
                ),
            )
        )

    for match in NUMBER_RE.finditer(line):
        if overlaps(match.span(), suffix_spans):
            continue
        token = match.group("token")
        if is_unmatched_parenthetical_fragment(token):
            continue
        value = parse_decimal_token(token, line)
        if value is None:
            continue
        adjusted, adjustment, classification = adjust_candidate(value, token, line, unit)
        unit_label = unit.source_text if unit else None
        candidates.append(
            NumericCandidate(
                page=page_number,
                line_number=line_number,
                token=token.strip(),
                raw_value=value,
                adjusted_value=adjusted,
                context=line.strip(),
                unit=unit_label,
                adjustment=adjustment,
                classification=classification,
                verification_path=build_verification_path(
                    page_number,
                    line_number,
                    line,
                    token,
                    match.span(),
                    all_lines,
                    classification,
                ),
            )
        )

    return candidates


def ensure_ocr_available() -> None:
    if pytesseract is None:
        raise RuntimeError("OCR is needed for image-only pages; install dependencies with `pip install -r requirements.txt`.")
    if pdfium is None:
        raise RuntimeError("OCR is needed for image-only pages; install pypdfium2 with `pip install -r requirements.txt`.")
    if shutil.which("tesseract") is None:
        raise RuntimeError("OCR is needed for image-only pages; install the Tesseract binary, e.g. `brew install tesseract`.")


def ocr_page_lines(ocr_pdf, page_index: int) -> List[str]:
    """OCR an image-only page with local Tesseract."""
    image = ocr_pdf[page_index].render(scale=3).to_pil()
    text = pytesseract.image_to_string(image)
    return [line for line in text.splitlines() if line.strip()]


def extract_candidates(pdf_path: Path) -> List[NumericCandidate]:
    """Scan every page, using embedded text first and OCR only when needed."""
    if pdfplumber is None:
        raise RuntimeError("Missing dependency: install pdfplumber with `pip install -r requirements.txt`.")

    candidates: List[NumericCandidate] = []
    ocr_pdf = None
    ocr_error: Optional[str] = None
    with pdfplumber.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            lines = [line for line in text.splitlines() if line.strip()]
            if not lines and page.images:
                try:
                    if ocr_pdf is None:
                        ensure_ocr_available()
                        ocr_pdf = pdfium.PdfDocument(str(pdf_path))
                    lines = ocr_page_lines(ocr_pdf, page_number - 1)
                except RuntimeError as exc:
                    ocr_error = str(exc)
            page_unit = choose_page_unit(lines)
            for line_number, line in enumerate(lines, start=1):
                candidates.extend(extract_line_candidates(page_number, line_number, line, page_unit, lines))
    if not candidates and ocr_error:
        raise RuntimeError(ocr_error)
    return candidates


def best_candidates(candidates: Sequence[NumericCandidate]) -> Tuple[NumericCandidate, NumericCandidate]:
    if not candidates:
        raise ValueError("No numeric candidates were found.")
    raw = max(candidates, key=lambda item: item.raw_value)
    adjusted = max(candidates, key=lambda item: item.adjusted_value)
    return raw, adjusted


def print_candidate(title: str, candidate: NumericCandidate) -> None:
    print(title)
    print(f"  value: {format_decimal(candidate.adjusted_value if title.endswith('adjusted') else candidate.raw_value)}")
    if title.endswith("adjusted"):
        print(f"  raw token: {candidate.token}")
        print(f"  raw value: {format_decimal(candidate.raw_value)}")
    else:
        print(f"  token: {candidate.token}")
    print(f"  page: {candidate.page}")
    print(f"  unit: {candidate.unit or 'none'}")
    print(f"  adjustment: {candidate.adjustment}")
    print(f"  classification: {candidate.classification}")
    print(f"  path: {candidate.verification_path or f'Page {candidate.page}'}")
    print(f"  context: {candidate.context}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Find the largest raw and unit-adjusted number in a PDF.")
    parser.add_argument("pdf", type=Path, help="Path to the PDF to scan.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human-readable output.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.pdf.exists():
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 2

    try:
        candidates = extract_candidates(args.pdf)
        raw, adjusted = best_candidates(candidates)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "largest_raw": raw.to_jsonable(),
            "largest_adjusted": adjusted.to_jsonable(),
            "candidate_count": len(candidates),
        }
        print(json.dumps(payload, indent=2))
        return 0

    print_candidate("Largest raw", raw)
    print()
    print_candidate("Largest adjusted", adjusted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

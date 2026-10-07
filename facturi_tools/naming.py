"""Validation and file naming for a read invoice. Pure functions, standard library only."""
import datetime
import math
import os
import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal

MAX_NAME = 120
REVIEW_FOLDER = "De verificat"
_FORBIDDEN = re.compile(r'[/\\:*?"<>|]')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_TOTAL = 1e12


def sanitize(text):
    """ASCII-fold a name part, drop forbidden and control characters, collapse whitespace to one hyphen.

    A non-string is treated as empty so that a wrong-typed reader value never raises.
    """
    if not isinstance(text, str):
        return ""
    s = unicodedata.normalize("NFKD", text)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = _FORBIDDEN.sub("", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = _CONTROL.sub("", s)
    return s.strip("-.")


def _valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_total(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return False
    try:
        f = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(f) and abs(f) < MAX_TOTAL


def _fmt_total(value):
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _digits(value):
    return re.sub(r"[^0-9]", "", str(value))


def _buyer_vat_ok(vat, buyer_cui=None):
    """True/False against the configured buyer number, None when it cannot be checked.

    buyer_cui defaults to the FACTURI_BUYER_CUI environment variable, read at call time.
    """
    if buyer_cui is None:
        buyer_cui = os.environ.get("FACTURI_BUYER_CUI")
    expected = _digits(buyer_cui) if buyer_cui is not None else ""
    if not expected or vat is None:
        return None
    digits = _digits(vat)
    if not digits:
        return None
    return digits == expected


def _cut(text, n):
    return text[:max(n, 1)].strip("-.") or text[:1]


def _sha8(sha256):
    if isinstance(sha256, str) and re.fullmatch(r"[0-9a-fA-F]{8,}", sha256):
        return sha256[:8]
    return "00000000"


def _review_name(sha256, original_name):
    clean = sanitize(original_name) or "file"
    if not clean.lower().endswith(".pdf"):
        clean += ".pdf"
    name = f"{_sha8(sha256)}_{clean}"
    if len(name) > MAX_NAME:
        name = name[:MAX_NAME - 4].rstrip("-.") + ".pdf"
    return name


def plan_invoice(reader, sha256, original_name, buyer_cui=None):
    if not isinstance(reader, dict):
        reader = {}
    supplier = sanitize(reader.get("supplier_short"))
    currency = reader.get("currency")
    missing = []
    if not supplier:
        missing.append("supplier_short")
    if not _valid_date(reader.get("issue_date")):
        missing.append("issue_date")
    if not _valid_total(reader.get("total")):
        missing.append("total")
    if not (isinstance(currency, str) and re.fullmatch(r"[A-Za-z]{3}", currency.strip())):
        missing.append("currency")

    result = {"status": "ok", "missing": missing, "buyerVatOk": _buyer_vat_ok(reader.get("buyer_vat_id"), buyer_cui)}
    if missing:
        name = _review_name(sha256, original_name)
        result.update(status="needs_review", folder=REVIEW_FOLDER, fileName=name,
                      path=f"{REVIEW_FOLDER}/{name}")
        return result

    date = reader["issue_date"]
    number = sanitize(reader.get("invoice_number")) \
        or sanitize(reader.get("payment_reference")) \
        or f"noNR-{_sha8(sha256)}"
    tail = f"_{_fmt_total(reader['total'])}{currency.strip().upper()}.pdf"
    budget = MAX_NAME - len(date) - 2 - len(tail)  # two separators around supplier and number
    over = len(supplier) + len(number) - budget
    if over > 0:
        cut_sup = min(over, max(len(supplier) - 8, 0))
        supplier = _cut(supplier, len(supplier) - cut_sup)
        over -= cut_sup
    if over > 0:
        number = _cut(number, len(number) - over)

    name = f"{date}_{supplier}_{number}{tail}"
    folder = f"{date[:4]}/{date[:7]}"
    result.update(fileName=name, folder=folder, path=f"{folder}/{name}")
    return result

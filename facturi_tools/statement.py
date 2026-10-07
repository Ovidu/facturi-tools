"""Deterministic parser for ING Romania account statements (pymupdf text, no LLM)."""
import re
from decimal import Decimal, InvalidOperation

TOL = Decimal("0.005")
AMOUNT_RE = re.compile(r"^-?\d{1,3}(?:,\d{3})*\.\d{2}$")
DATE_RE = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})$")
REF_RE = re.compile(r"^\d{4,6}$")
IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")
TYPE_RE = re.compile(r"^(Cumparare|Incasare|Transfer|Comision|Taxa|Plata|Retragere|Depunere)\b")
FOOTER_END = "sunt disponibile pe www.ing.ro/dgs"
END_MARK = "SFARSIT EXTRAS DE CONT"


def parse_amount(text):
    """'-1,234.56' -> -1234.56. None when the whole line is not an amount."""
    t = text.strip()
    if not AMOUNT_RE.match(t):
        return None
    return float(Decimal(t.replace(",", "")))


def iso_date(dmy):
    m = DATE_RE.match(dmy.strip())
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def extract_pages(pdf_bytes):
    import pymupdf
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return [[ln.strip() for ln in page.get_text().split("\n")] for page in doc]
    finally:
        doc.close()


def strip_page(lines, first):
    """Drop the repeated page header and footer. They can sit inside a block that
    crosses a page break, so they must go before blocks are split."""
    out = list(lines)
    for i, ln in enumerate(out):
        if ln.startswith(FOOTER_END):
            out = out[i + 1:]
            break
    if first:
        for i, ln in enumerate(out):
            if ln == "Descrierea tranzactiei":
                out = out[i + 1:]
                break
    return out


def body_lines(pages):
    body = []
    for n, page in enumerate(pages):
        body.extend(strip_page(page, n == 0))
    for i, ln in enumerate(body):
        if ln.startswith(END_MARK):
            return body[:i]
    return body


def _dec(x):
    return Decimal(str(x))


def parse_header(first_page):
    h = {}
    text = "\n".join(first_page)
    m = re.search(r"Nr\.\s*(\d+)\s*/\s*(\d{2}\.\d{2}\.\d{4})", text)
    if m:
        h["statementNo"] = m.group(1)
        h["statementDate"] = iso_date(m.group(2))
    m = re.search(r"\b(RO\d{2}) ?(INGB) ?(\d{4}) ?(\d{4}) ?(\d{4}) ?(\d{4})\b", text)
    if m:
        h["iban"] = "".join(m.groups())
    m = re.search(r"\|\s*([A-Z]{3})\s*$", text, re.M)
    h["currency"] = m.group(1) if m else None
    m = re.search(r"Total creditari \((\d+)\)", text)
    h["nCredits"] = int(m.group(1)) if m else None
    m = re.search(r"Total debitari \((\d+)\)", text)
    h["nDebits"] = int(m.group(1)) if m else None
    m = re.search(r"(\d{2}\.\d{2}\.\d{4}) - (\d{2}\.\d{2}\.\d{4})", text)
    if m:
        h["periodStart"], h["periodEnd"] = iso_date(m.group(1)), iso_date(m.group(2))
    else:
        m = re.search(r"(\d{2}) - (\d{2}\.\d{2}\.\d{4})", text)
        if m:
            end = iso_date(m.group(2))
            h["periodEnd"] = end
            h["periodStart"] = f"{end[:8]}{m.group(1)}"
    if "Perioada" in first_page:
        i = first_page.index("Perioada")
        nums = [parse_amount(x) for x in first_page[i + 1:i + 12] if parse_amount(x) is not None]
        if len(nums) >= 4:
            h["opening"], h["totalCredits"], h["totalDebits"], h["closing"] = nums[:4]
    return h


def split_blocks(lines):
    """A block starts at a DD.MM.YYYY line followed by a 4-6 digit bank reference."""
    starts = [i for i in range(len(lines) - 1)
              if DATE_RE.match(lines[i]) and REF_RE.match(lines[i + 1])]
    blocks = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(lines)
        blocks.append(lines[s:e])
    return blocks


def _type_index(desc):
    """Index of the bank's own operation-type line. Line 0 is the counterparty unless the
    block has no other type line (a bare fee), and the search starts after the last IBAN or
    CUI line, so a counterparty named 'Comision Expert SRL' or free text such as
    'Taxa locala' after the type line cannot change the kind. The statements differ in
    whether a blank line separates the party from the type, so no blank is assumed."""
    marks = [i for i, ln in enumerate(desc) if IBAN_RE.match(ln) or ln.startswith("CUI:")]
    start = marks[-1] + 1 if marks else 1
    for i in range(start, len(desc)):
        if TYPE_RE.match(desc[i]):
            return i
    for i in range(0, min(start, len(desc))):
        if TYPE_RE.match(desc[i]):
            return i
    return None


def _counterparty(party, card):
    party = [x for x in party]
    while party and not party[0]:
        party.pop(0)
    if not party:
        return ""
    if card:
        return party[0]
    parts = []
    for ln in party:
        if not ln or IBAN_RE.match(ln) or ln.startswith("CUI:") or parse_amount(ln) is not None:
            break
        parts.append(ln)
    return " ".join(parts)


def _kind(line):
    """Kind comes from the bank's own type line only."""
    if line.startswith("Cumparare POS"):
        return "card"
    if line.startswith("Incasare"):
        return "incoming"
    if line.startswith(("Comision", "Taxa")):
        return "fee"
    if line.startswith("Transfer"):
        return "transfer"
    return "other"


def parse_block(block, index):
    rest = block[2:]
    # The amount is the last pair of consecutive amount lines (amount, then running balance).
    # A description can wrap an amount-like fragment such as "12.20", so the first amount-like
    # line is not safe.
    amount_i = None
    for i in range(len(rest) - 2, -1, -1):
        if parse_amount(rest[i]) is not None and parse_amount(rest[i + 1]) is not None:
            amount_i = i
            break
    amount = parse_amount(rest[amount_i]) if amount_i is not None else None
    balance = parse_amount(rest[amount_i + 1]) if amount_i is not None else None
    desc = rest[:amount_i] if amount_i is not None else rest
    ti = _type_index(desc)
    kind = _kind(desc[ti]) if ti is not None else "other"
    card = kind == "card"
    cp = _counterparty(desc[:ti] if ti is not None else desc, card)
    t = {"index": index, "procDate": iso_date(block[0]), "bankRef": block[1],
         "counterparty": cp, "kind": kind, "amountRon": amount, "balance": balance,
         "cardDate": None, "origAmount": None, "origCurrency": None, "fxRate": None,
         "ibanCountry": None, "descriptorRef": None}
    for ln in desc:
        m = re.match(r"^Data:\s*(\d{2}-\d{2}-\d{4})\s+Autorizare", ln)
        if m:
            t["cardDate"] = iso_date(m.group(1).replace("-", "."))
        m = re.match(r"^Suma:\s*([\d.]+),(\d{2})\s+([A-Z]{3})$", ln.replace(" ", " "))
        if m:
            t["origAmount"] = float(m.group(1).replace(".", "") + "." + m.group(2))
            t["origCurrency"] = m.group(3)
        m = re.match(r"^Rata:\s*([\d.]+)$", ln)
        if m:
            t["fxRate"] = float(m.group(1))
        if t["ibanCountry"] is None and IBAN_RE.match(ln):
            t["ibanCountry"] = ln[:2]
    if "*" in cp:
        t["descriptorRef"] = cp.split("*", 1)[1].strip() or None
    return t


def verify(header, txs):
    """None when the statement adds up, otherwise a sentence naming the first failed check."""
    need = ("opening", "closing", "totalCredits", "totalDebits", "nCredits", "nDebits")
    if any(header.get(k) is None for k in need):
        return "Statement header is incomplete, so totals cannot be checked."
    if any(t.get("amountRon") is None for t in txs):
        return "A transaction has no readable amount."
    cr = [_dec(t["amountRon"]) for t in txs if t["amountRon"] > 0]
    db = [_dec(t["amountRon"]) for t in txs if t["amountRon"] < 0]
    if len(cr) != header["nCredits"]:
        return f"Credit count is {len(cr)} but the header says {header['nCredits']}."
    if len(db) != header["nDebits"]:
        return f"Debit count is {len(db)} but the header says {header['nDebits']}."
    if abs(sum(cr, Decimal(0)) - _dec(header["totalCredits"])) > TOL:
        return f"Credit sum is {sum(cr, Decimal(0)):.2f} but the header says {header['totalCredits']:.2f}."
    if abs(sum(db, Decimal(0)) - _dec(header["totalDebits"])) > TOL:
        return f"Debit sum is {sum(db, Decimal(0)):.2f} but the header says {header['totalDebits']:.2f}."
    calc = _dec(header["opening"]) + _dec(header["totalCredits"]) + _dec(header["totalDebits"])
    if abs(calc - _dec(header["closing"])) > TOL:
        return f"Opening plus credits plus debits is {calc:.2f} but closing is {header['closing']:.2f}."
    return _verify_rows(header, txs)


def _where(t, i):
    return f"transaction {t.get('index', i)} ({t.get('procDate')}, ref {t.get('bankRef')})"


def _verify_rows(header, txs):
    """Per-transaction checks: sign against kind, and the running-balance chain."""
    prev = _dec(header["opening"])
    for i, t in enumerate(txs):
        kind, amt = t.get("kind"), t["amountRon"]
        if kind in ("card", "fee", "transfer") and amt >= 0:
            return f"{_where(t, i)} is a {kind} but its amount is {amt:.2f}, not negative."
        if kind == "incoming" and amt <= 0:
            return f"{_where(t, i)} is incoming but its amount is {amt:.2f}, not positive."
        if "balance" in t:
            if t["balance"] is None:
                return f"{_where(t, i)} has no readable running balance."
            expect = prev + _dec(amt)
            if abs(expect - _dec(t["balance"])) > TOL:
                return (f"Running balance breaks at {_where(t, i)}: "
                        f"expected {expect:.2f} but the statement shows {t['balance']:.2f}.")
            prev = _dec(t["balance"])
    if txs and "balance" in txs[-1] and abs(prev - _dec(header["closing"])) > TOL:
        return f"Last running balance is {prev:.2f} but closing is {header['closing']:.2f}."
    return None


def parse_pages(pages):
    """Parse already-extracted text: a list of pages, each a list of stripped lines."""
    empty = {"statementNo": None, "statementDate": None, "periodStart": None, "periodEnd": None,
             "iban": None, "currency": None, "opening": None, "closing": None,
             "totalCredits": None, "nCredits": None, "totalDebits": None, "nDebits": None,
             "verified": False, "verifyError": None, "transactions": []}
    if not pages or not any(ln == "Extras de cont" for ln in pages[0][:3]):
        empty["verifyError"] = "The document is not an ING account statement."
        return empty
    res = {**empty, **parse_header(pages[0])}
    txs = [parse_block(b, i) for i, b in enumerate(split_blocks(body_lines(pages)))]
    res["transactions"] = txs
    err = verify(res, txs)
    res["verified"] = err is None
    res["verifyError"] = err
    return res


def parse_statement(pdf_bytes):
    try:
        pages = extract_pages(pdf_bytes)
    except Exception:
        res = parse_pages([])
        res["verifyError"] = "The file could not be read as a PDF."
        return res
    return parse_pages(pages)

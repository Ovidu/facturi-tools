"""NocoDB ledger client: suppliers, invoices, statements and transactions.

Field names are exactly the column names created by the setup script. NocoDB gives decimals
back as strings and booleans as true/false or 1/0, so rows are normalised on the way out.
Lookups send a `where` filter to keep the answer small and then compare client-side as well,
so a filter that matches loosely can never return the wrong row.
"""
import json

from facturi_tools import httpx

PAGE_SIZE = 100
INSERT_CHUNK = 50
DECIMALS = ("total", "vat_amount", "amount_ron", "orig_amount", "total_debits", "total_credits")
BOOLEANS = ("buyer_vat_ok", "verified")
# Characters that break NocoDB's where syntax: such a value is filtered client-side only.
UNSAFE = set(",()~")


class LedgerError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _empty(value):
    return value is None or value == ""


def _bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _float(value):
    if _empty(value) or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise(row):
    out = dict(row)
    for k in DECIMALS:
        if k in out:
            out[k] = _float(out[k])
    for k in BOOLEANS:
        if k in out:
            out[k] = _bool(out[k])
    return out


class Ledger:
    def __init__(self, cfg, http=httpx.request):
        self._http = http
        self._base = (cfg.get("NOCODB_URL") or "").rstrip("/")
        self._token = cfg.get("NOCODB_TOKEN") or ""
        self._t = {"invoices": cfg.get("NOCODB_TABLE_INVOICES"), "suppliers": cfg.get("NOCODB_TABLE_SUPPLIERS"),
                   "transactions": cfg.get("NOCODB_TABLE_TRANSACTIONS"),
                   "statements": cfg.get("NOCODB_TABLE_STATEMENTS")}

    # ---- plumbing ----

    def _scrub(self, text):
        return text.replace(self._token, "***") if self._token else text

    def _fail(self, status, text):
        label = f"HTTP {status}" if status is not None else "NocoDB"
        return LedgerError(self._scrub(f"{label} {text}".strip()), status=status)

    def _call(self, method, table, params=None, json_body=None):
        url = f"{self._base}/api/v2/tables/{self._t[table]}/records"
        try:
            status, _, body = self._http(method, url, headers={"xc-token": self._token},
                                         params=params, json_body=json_body)
        except httpx.NetworkError as e:
            raise self._fail(None, f"network error: {e}")
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if not 200 <= status < 300:  # a redirect is a failure: httpx returns 3xx instead of following it
            text = ""
            if isinstance(parsed, dict):
                text = str(parsed.get("msg") or parsed.get("message") or parsed.get("error") or "")
            raise self._fail(status, text or "request failed")
        if parsed is None:
            raise self._fail(status, "answer is not JSON")
        return parsed

    def _list(self, table, where=None):
        rows, offset = [], 0
        while True:
            params = {"limit": PAGE_SIZE, "offset": offset}
            if where:
                params["where"] = where
            page = self._call("GET", table, params=params)
            batch = page.get("list") if isinstance(page, dict) else None
            if not isinstance(batch, list):
                raise self._fail(None, "unexpected list answer")
            rows.extend(_normalise(r) for r in batch if isinstance(r, dict))
            info = page.get("pageInfo")
            last = info.get("isLastPage", True) if isinstance(info, dict) else len(batch) < PAGE_SIZE
            if not batch or last:
                return rows
            offset += len(batch)

    def _select(self, table, conds, keep=None):
        """Rows whose every (field, value) condition holds exactly, filtered by NocoDB where it is safe."""
        if any(_empty(v) or not str(v).strip() for _, v in conds):
            return []  # a null or empty lookup value matches nothing, not the rows with a null field
        parts = [f"({f},eq,{v})" for f, v in conds if not (set(str(v)) & UNSAFE)]
        rows = self._list(table, "~and".join(parts) or None)
        rows = [r for r in rows if all(r.get(f) is not None and str(r.get(f)) == str(v) for f, v in conds)]
        return [r for r in rows if keep(r)] if keep else rows

    def _first(self, table, conds):
        rows = self._select(table, conds)
        return rows[0] if rows else None

    def _add(self, table, row):
        out = self._call("POST", table, json_body=dict(row))
        if not isinstance(out, dict) or _empty(out.get("Id")):
            raise self._fail(None, "insert answer has no Id, the row may not have been written")
        merged = dict(row)
        merged.update(out)
        return _normalise(merged)

    def _update(self, table, row_id, fields):
        body = dict(fields)
        body["Id"] = row_id
        self._call("PATCH", table, json_body=body)

    # ---- suppliers ----

    def suppliers(self):
        return self._list("suppliers")

    def add_supplier(self, row):
        return self._add("suppliers", row)

    # ---- invoices ----

    def find_invoice_by_hash(self, sha256):
        return self._first("invoices", [("sha256", sha256)])

    def find_invoice_by_number(self, supplier_short, invoice_number):
        if any(_empty(v) or not str(v).strip() for v in (supplier_short, invoice_number)):
            return None
        # The reader spells a supplier with varying letter case, so the supplier is compared here,
        # ignoring case and surrounding spaces; only the number goes to the server.
        want = str(supplier_short).strip().lower()
        rows = self._select("invoices", [("invoice_number", str(invoice_number).strip())],
                            keep=lambda r: str(r.get("supplier_short") or "").strip().lower() == want)
        return rows[0] if rows else None

    def find_invoice_by_matched_tx(self, tx_id):
        return self._first("invoices", [("matched_tx", tx_id)])

    def add_invoice(self, row):
        return self._add("invoices", row)

    def update_invoice(self, row_id, fields):
        self._update("invoices", row_id, fields)

    def unmatched_invoices(self):
        return self._select("invoices", [("status", "filed")], keep=lambda r: _empty(r.get("matched_tx")))

    # ---- statements ----

    def find_statement_by_hash(self, sha256):
        return self._first("statements", [("sha256", sha256)])

    def find_statement_by_period(self, statement_no, period_start, period_end):
        if any(_empty(v) or not str(v).strip() for v in (statement_no, period_start, period_end)):
            return None
        # NocoDB refuses a plain eq on a Date column, so only the text column goes to the server
        # and the dates are compared here, on their first 10 characters (YYYY-MM-DD).
        def same_dates(r):
            return (str(r.get("period_start") or "")[:10] == str(period_start)[:10]
                    and str(r.get("period_end") or "")[:10] == str(period_end)[:10])
        rows = self._select("statements", [("statement_no", statement_no)], keep=same_dates)
        return rows[0] if rows else None

    def add_statement(self, row):
        return self._add("statements", row)

    def update_statement(self, row_id, fields):
        self._update("statements", row_id, fields)

    def open_statements(self):
        return [r for r in self._list("statements") if _empty(r.get("completed_at"))]

    # ---- transactions ----

    def add_transactions(self, rows):
        rows = list(rows)
        out = []
        for i in range(0, len(rows), INSERT_CHUNK):
            chunk = rows[i:i + INSERT_CHUNK]
            answer = self._call("POST", "transactions", json_body=[dict(r) for r in chunk])
            if (not isinstance(answer, list) or len(answer) != len(chunk)
                    or not all(isinstance(g, dict) and not _empty(g.get("Id")) for g in answer)):
                raise self._fail(None, "unexpected bulk insert answer, rows may not have been written")
            for row, got in zip(chunk, answer):
                merged = dict(row)
                merged.update(got)
                out.append(_normalise(merged))
        return out

    def transactions_for_statement(self, statement_id):
        return self._select("transactions", [("statement", statement_id)])

    def open_transactions(self):
        return self._select("transactions", [("status", "missing")])

    def update_transaction(self, row_id, fields):
        self._update("transactions", row_id, fields)

    def count_open_for_statement(self, statement_id):
        return len(self._select("transactions", [("statement", statement_id), ("status", "missing")]))

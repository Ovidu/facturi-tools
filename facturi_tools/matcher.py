"""Decide, for each card payment, whether a supplier invoice is on file.

Pure function: plain lists and dicts in, a dict out, no I/O. Inputs are parsed statement
transactions, invoice records and a supplier table with one matching rule per supplier.

Guiding principle: a payment is `matched` only against an invoice owned by its own
supplier. Anything else is at most a `possible_match` shown to the owner. A false
"missing" costs a glance, a false "matched" hides a missing invoice.
"""
import re
from datetime import date
from decimal import Decimal, InvalidOperation

TOLERANCE = Decimal("0.005")
WINDOW_DAYS = 10


def _dec(value):
    """Number or numeric string to Decimal, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _str(value):
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value))


def _flag(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _terms(invoice_match):
    return [t.strip().lower() for t in _str(invoice_match).split(",") if t.strip()]


def _hay(invoice):
    return (_str(invoice.get("supplier_short")) + " " + _str(invoice.get("supplier_name"))).lower()


def _has_alias(alias, hay):
    """Whole word or phrase: the neighbours must be an edge or a non-alphanumeric ASCII char."""
    return re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", hay) is not None


def _owner(invoice, active):
    """supplier_short of the one active supplier owning this invoice, or None.

    Longest whole-word alias wins, so an invoice from "Acme Cloud" is not owned by "Acme" and
    "Shoemaker" is not owned by anybody. A tie at the longest length between two
    different suppliers leaves the invoice unattributed.
    """
    hay = _hay(invoice)
    best = {}
    for s in active:
        longest = max((len(t) for t in _terms(s.get("invoice_match")) if _has_alias(t, hay)), default=0)
        if longest:
            short = _str(s.get("supplier_short"))
            best[short] = max(best.get(short, 0), longest)
    if not best:
        return None
    top = max(best.values())
    winners = [k for k, v in best.items() if v == top]
    return winners[0] if len(winners) == 1 else None


def owner_of(invoice, suppliers):
    """supplier_short of the active supplier that owns this invoice, or None.

    Public wrapper over the matcher's own ownership rule, so the pipeline and the matcher
    cannot disagree about who an invoice belongs to.
    """
    if not isinstance(invoice, dict):
        return None
    active = [s for s in (suppliers or []) if isinstance(s, dict) and s.get("status") == "active"]
    return _owner(invoice, active)


def _find_supplier(counterparty, active):
    cp = _str(counterparty).lower()
    best = None
    for s in active:
        pat = _str(s.get("descriptor_pattern")).lower()
        if not pat or pat not in cp:
            continue
        key = (-len(pat), 1 if s.get("rule") == "skip" else 0, _str(s.get("supplier_short")))
        if best is None or key < best[0]:
            best = (key, s)
    return best[1] if best else None


def _classify(t):
    """Return (checked, amount_decimal_or_None, invalid_flag)."""
    if not isinstance(t, dict):
        return False, None, False
    kind = t.get("kind")
    if kind == "card":
        amount = _dec(t.get("amountRon"))
        if amount is None:
            return True, None, True
        return amount < 0, amount, False
    if kind == "transfer":
        country = t.get("ibanCountry")
        if not isinstance(country, str) or not country.strip() or country.strip().upper() == "RO":
            return False, None, False
        amount = _dec(t.get("amountRon"))
        return True, amount, amount is None
    return False, None, False


def _need(t, amount_ron):
    """Amount and currency the invoice must carry, or None when unusable."""
    if t.get("origAmount") is not None and _str(t.get("origCurrency")):
        amt = _dec(t["origAmount"])
        return (abs(amt), _str(t["origCurrency"]).upper()) if amt is not None else None
    return abs(amount_ron), "RON"


def _tx_date(t):
    return _date(t.get("cardDate")) or _date(t.get("procDate"))


def _pick(candidates, when):
    """Closest date, then issue_date, then id as string."""
    best, best_key = None, None
    for inv in candidates:
        issued = _date(inv.get("issue_date"))
        key = (abs((issued - when).days), _str(inv.get("issue_date")), str(inv.get("id")))
        if best_key is None or key < best_key:
            best, best_key = inv, key
    return best


def _fits(inv, need, when):
    if need is None or when is None or inv["_matched"] or not inv["_usable"]:
        return False
    total, issued = _dec(inv.get("total")), _date(inv.get("issue_date"))
    if total is None or issued is None:
        return False
    if _str(inv.get("currency")).upper() != need[1] or abs(total - need[0]) > TOLERANCE:
        return False
    return abs((issued - when).days) <= WINDOW_DAYS


def _proposed_pattern(counterparty):
    return re.sub(r"\s+", " ", re.sub(r"\d{6,}", "", _str(counterparty))).strip()


def match(transactions, invoices, suppliers, statement):
    suppliers = [s for s in (suppliers or []) if isinstance(s, dict)]
    active = [s for s in suppliers if s.get("status") == "active"]
    invs = []
    for i in invoices or []:
        if isinstance(i, dict):
            c = dict(i)
            c["_matched"] = _flag(i.get("matched"))
            c["_owner"] = _owner(i, active)
            c["_usable"] = i.get("id") is not None and _str(i.get("id")) != ""
            invs.append(c)

    txs = list(transactions or [])
    checked = []  # (pos, tx, amount, invalid)
    for pos, t in enumerate(txs):
        ok, amount, invalid = _classify(t)
        if ok:
            checked.append((pos, t, amount, invalid))

    out = {}
    proposed = []

    def record(pos, t, status, reason, short, inv=None, cand=None):
        out[pos] = {
            "txPos": pos, "txIndex": t.get("index"), "txId": t.get("id"), "status": status,
            "reason": reason, "supplierShort": short,
            "invoiceId": inv.get("id") if inv else None,
            "candidateInvoiceId": cand.get("id") if cand else None,
        }

    canon = sorted(checked, key=lambda c: (_str(c[1].get("cardDate") or c[1].get("procDate")),
                                           _str(c[1].get("bankRef")), c[0]))
    sup_of = {c[0]: _find_supplier(c[1].get("counterparty"), active) for c in checked}

    def own_candidates(sup, need, when):
        return [i for i in invs if i["_owner"] == _str(sup.get("supplier_short")) and _fits(i, need, when)]

    def unattributed(need, when):
        return _pick([i for i in invs if i["_owner"] is None and _fits(i, need, when)], when)

    # Pass 1: reference lines claim their invoice before anything else can take it.
    for pos, t, amount, invalid in canon:
        sup = sup_of[pos]
        if invalid or t["kind"] != "card" or not sup or sup.get("rule") != "reference":
            continue
        ref = _str(t.get("descriptorRef")).lower()
        if not ref:
            continue
        hits = [i for i in invs if not i["_matched"] and i["_usable"] and i["_owner"] == _str(sup.get("supplier_short"))
                and _str(i.get("payment_reference")).lower() == ref]
        if hits:
            inv = _pick_ref(hits, _tx_date(t))
            inv["_matched"] = True
            record(pos, t, "matched", "reference", _str(sup.get("supplier_short")), inv)

    # Pass 2: everything else, in canonical order.
    for pos, t, amount, invalid in canon:
        if pos in out:
            continue
        sup = sup_of[pos]
        short = _str(sup.get("supplier_short")) if sup else None
        if invalid:
            record(pos, t, "missing", "invalid_transaction", short)
            continue
        need = _need(t, amount)
        if need is None:
            record(pos, t, "missing", "invalid_transaction", short)
            continue
        when = _tx_date(t)

        if t["kind"] == "transfer" and sup is None:
            record(pos, t, "missing", "foreign_transfer", None)
            continue

        if sup is None:
            cand = unattributed(need, when)
            if cand is None:
                record(pos, t, "missing", "new_supplier", None)
                continue
            record(pos, t, "missing", "possible_match", None, cand=cand)
            pattern = _proposed_pattern(t.get("counterparty"))
            if pattern and not any(p["descriptor_pattern"] == pattern for p in proposed):
                proposed.append({"supplier_short": _str(cand.get("supplier_short")),
                                 "descriptor_pattern": pattern,
                                 "invoice_match": _str(cand.get("supplier_short")).lower(),
                                 "rule": "exact"})
            continue

        rule = sup.get("rule")
        if t["kind"] == "transfer":
            inv = _pick(own_candidates(sup, need, when), when) if rule != "monthly" else None
            if inv:
                inv["_matched"] = True
                record(pos, t, "matched", "exact", short, inv)
            else:
                record(pos, t, "missing", "foreign_transfer", short)
        elif rule == "skip":
            record(pos, t, "skipped", "rule_skip", short)
        elif rule == "monthly":
            proc = _date(t.get("procDate"))
            owned = [i for i in invs if i["_owner"] == short and i["_usable"] and proc is not None
                     and (_date(i.get("issue_date")) or date.min).replace(day=1) == proc.replace(day=1)]
            inv = _pick(owned, proc) if owned else None
            if inv:
                record(pos, t, "matched", "monthly", short, inv)
            else:
                record(pos, t, "missing", "no_invoice", short)
        else:
            # exact, a reference line with no reference hit, and expected_no_payment
            # (a card payment there is unexpected, so it must still find an invoice).
            inv = _pick(own_candidates(sup, need, when), when)
            if inv:
                inv["_matched"] = True
                record(pos, t, "matched", "exact", short, inv)
            else:
                cand = unattributed(need, when)
                if cand:
                    record(pos, t, "missing", "possible_match", short, cand=cand)
                else:
                    record(pos, t, "missing", "no_invoice", short)

    results = [out[c[0]] for c in checked]

    expected_missing = []
    month = None
    if isinstance(statement, dict):
        end = _date(statement.get("periodEnd")) or _date(statement.get("periodStart"))
        month = end.strftime("%Y-%m") if end else None
    if month:
        for s in sorted(active, key=lambda x: _str(x.get("supplier_short"))):
            if s.get("rule") != "expected_no_payment":
                continue
            short = _str(s.get("supplier_short"))
            if not any(i["_owner"] == short and (d := _date(i.get("issue_date")))
                       and d.strftime("%Y-%m") == month for i in invs):
                expected_missing.append({"supplierShort": short, "month": month})

    summary = {
        "checked": len(results),
        "matched": sum(r["status"] == "matched" for r in results),
        "missing": sum(r["status"] == "missing" for r in results),
        "skipped": sum(r["status"] == "skipped" for r in results),
    }
    return {"results": results, "expectedMissing": expected_missing,
            "proposedSuppliers": proposed, "summary": summary}


def _pick_ref(hits, when):
    if when is None:
        return min(hits, key=lambda i: (_str(i.get("issue_date")), str(i.get("id"))))
    scored = [i for i in hits if _date(i.get("issue_date"))]
    if not scored:
        return min(hits, key=lambda i: str(i.get("id")))
    return _pick(scored, when)

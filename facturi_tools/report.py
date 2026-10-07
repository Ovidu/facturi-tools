"""The report email, built from a pipeline result. Romanian, plain sentences, no marketing tone.

It names suppliers, amounts and files only from the run data. Nothing else is hard-coded.
"""
from html import escape

MONTHS = ["ianuarie", "februarie", "martie", "aprilie", "mai", "iunie", "iulie", "august",
          "septembrie", "octombrie", "noiembrie", "decembrie"]

STYLE_H = "font-size:15px;margin:18px 0 6px;"
STYLE_P = "margin:4px 0;"
STYLE_T = "border-collapse:collapse;font-size:13px;"
STYLE_C = "border:1px solid #ccc;padding:4px 8px;text-align:left;"


def _month_name(period):
    try:
        return MONTHS[int(str(period)[5:7]) - 1]
    except (ValueError, IndexError, TypeError):
        return None


def _money(amount, currency):
    if amount is None:
        return "suma necunoscută"
    return f"{float(amount):.2f} {currency or ''}".strip()


def _reason(g):
    reason = g.get("reason")
    if reason == "no_invoice":
        return "fără factură"
    if reason == "new_supplier":
        return "furnizor nou"
    if reason == "possible_match":
        return f"posibilă potrivire: {g.get('candidate') or 'o factură nepotrivită'}"
    if reason == "foreign_transfer":
        return "transfer extern"
    if reason == "expected_no_payment":
        return "document lunar așteptat"
    if reason == "invalid_transaction":
        return "rând invalid în extras"
    return str(reason or "fără factură")


def _is_expected(g):
    return g.get("kind") == "expected" or g.get("reason") == "expected_no_payment"


def _plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def _sections(result):
    """List of (title, rows, columns). Empty sections are left out."""
    items = result.get("items") or []
    by = lambda o: [i for i in items if i.get("outcome") == o]  # noqa: E731
    out = []
    errors = result.get("errors") or []
    if errors:
        out.append(("Erori", [[e.get("where"), e.get("message")] for e in errors], ["Unde", "Ce s-a întâmplat"]))
    filed = by("filed")
    if filed:
        out.append(("Facturi arhivate", [[i.get("issueDate"), i.get("supplier"), i.get("invoiceNumber"),
                                          _money(i.get("total"), i.get("currency")), i.get("path")]
                                         for i in filed],
                    ["Data", "Furnizor", "Număr", "Total", "Fișier"]))
    review = by("needs_review") + by("statement_unverified")
    if review:
        rows = []
        for i in review:
            what = i.get("supplier") or i.get("attachment")
            why = i.get("note") or "de verificat"
            rows.append([what, why, i.get("path")])
        out.append(("De verificat", rows, ["Document", "Motiv", "Fișier"]))
    vat = [i for i in items if i.get("buyerVatOk") is False]
    if vat:
        out.append(("CUI diferit pe factură",
                    [[i.get("supplier"), i.get("buyerVatId"),
                      "CUI-ul cumpărătorului de pe factură nu este al firmei."] for i in vat],
                    ["Furnizor", "CUI pe factură", "Observație"]))
    others = [i for i in items if i.get("otherAttachments")]
    nopdf = by("no_pdf")
    if nopdf:
        out.append(("Mesaje fără PDF", [[i.get("from"), i.get("subject")] for i in nopdf], ["De la", "Subiect"]))
    if others:
        seen, rows = set(), []
        for i in others:
            if i.get("messageId") in seen:
                continue
            seen.add(i.get("messageId"))
            rows.append([i.get("from"), i.get("subject"), ", ".join(i["otherAttachments"])])
        out.append(("Alte atașamente, neprocesate", rows, ["De la", "Subiect", "Atașamente"]))
    dup = by("duplicate")
    if dup:
        out.append(("Duplicate", [[i.get("attachment"), i.get("note")] for i in dup], ["Fișier", "Observație"]))
    stmts = result.get("statements") or []
    shown = {str(s.get("statementNo")) for s in stmts if s.get("added") or s.get("reminder")}
    proc = [s for s in stmts if s.get("added") or s.get("outcome") == "statement_unverified"]
    if proc:
        rows = []
        for s in proc:
            if s.get("outcome") == "statement_unverified":
                state = "neverificat"
            elif s.get("reopened"):
                state = "verificat, redeschis (rânduri lipsă adăugate)"
            else:
                state = "verificat"
            rows.append([s.get("statementNo"), _month_name(s.get("period")) or "perioadă necunoscută", state,
                         _plural(s.get("checked") or 0, "plată verificată", "plăți verificate")])
        out.append(("Extras procesat", rows, ["Număr", "Perioada", "Stare", "Plăți"]))
    shown_gaps = [g for g in (result.get("gaps") or []) if str(g.get("statement")) in shown]
    gaps = [g for g in shown_gaps if not _is_expected(g)]
    docs = [g for g in shown_gaps if _is_expected(g)]
    if gaps:
        out.append(("Plăți fără factură", [[g.get("procDate"), g.get("counterparty"),
                                            _money(g.get("amount"), g.get("currency")), _reason(g)]
                                           for g in gaps], ["Data", "Comerciant", "Suma", "Motiv"]))
    if docs:
        out.append(("Documente lunare așteptate",
                    [[str(g.get("counterparty") or "").replace("(expected) ", "", 1), g.get("procDate"),
                      "document lunar așteptat, încă lipsește"] for g in docs],
                    ["Furnizor", "Luna", "Stare"]))
    matched = (result.get("counts") or {}).get("matched") or 0
    if matched and not proc:
        out.append(("Plăți potrivite acum", [[_plural(matched, "plată potrivită", "plăți potrivite")
                                              + " cu o factură."]], []))
    done = [s for s in stmts if s.get("complete")]
    if done:
        out.append(("Luni complete", [[(_month_name(s.get("period")) or "extras") + " este complet"
                                       + f" (extras {s.get('statementNo')})."] for s in done], []))
    rem = [s for s in stmts if s.get("reminder")]
    if rem:
        out.append(("Memento", [[f"Extrasul {s.get('statementNo')} ({_month_name(s.get('period')) or 'perioadă necunoscută'})"
                                 f" are încă {_plural(s.get('open') or 0, 'poziție deschisă', 'poziții deschise')}."]
                                for s in rem], []))
    prop = result.get("proposedSuppliers") or []
    if prop:
        out.append(("Furnizori noi propuși",
                    [[p.get("supplier_short"), p.get("descriptor_pattern"),
                      "Ca să îl activezi, pune statusul pe active în tabelul de furnizori."] for p in prop],
                    ["Furnizor", "Model în extras", "Ce faci"]))
    return out


def _subject(result):
    items = result.get("items") or []
    n = lambda o: sum(1 for i in items if i.get("outcome") == o)  # noqa: E731
    stmts = result.get("statements") or []
    parts = []
    if result.get("errors"):
        parts.append(_plural(len(result["errors"]), "eroare", "erori"))
    if n("filed"):
        parts.append(_plural(n("filed"), "arhivată", "arhivate"))
    review = n("needs_review") + n("statement_unverified")
    if review:
        parts.append(f"{review} de verificat")
    for s in stmts:
        month = _month_name(s.get("period")) or "extras"
        if s.get("complete"):
            parts.append(f"{month} complet")
        elif s.get("added"):
            parts.append(f"extras {month}")
    shown = {str(s.get("statementNo")) for s in stmts if s.get("added") or s.get("reminder")}
    shown_gaps = [g for g in (result.get("gaps") or []) if str(g.get("statement")) in shown]
    gaps = [g for g in shown_gaps if not _is_expected(g)]
    docs = [g for g in shown_gaps if _is_expected(g)]
    if gaps:
        parts.append(_plural(len(gaps), "plată fără factură", "plăți fără factură"))
    if docs:
        parts.append(_plural(len(docs), "document lunar lipsă", "documente lunare lipsă"))
    for s in stmts:
        if s.get("reminder") and not s.get("added"):
            parts.append(f"memento {_month_name(s.get('period')) or 'extras'}")
    if n("no_pdf"):
        parts.append(f"{n('no_pdf')} fără PDF")
    if n("duplicate"):
        parts.append(_plural(n("duplicate"), "duplicat", "duplicate"))
    if any(i.get("buyerVatOk") is False for i in items):
        parts.append("CUI diferit")
    if not parts and (result.get("counts") or {}).get("matched"):
        parts.append("plăți potrivite")
    return "Facturi: " + ", ".join(parts) if parts else None


def build_email(result):
    """`{"subject", "html", "text"}`, or None when no section has content."""
    if not isinstance(result, dict):
        return None
    sections = _sections(result)
    subject = _subject(result)
    if not sections or not subject:
        return None
    text, html = [], ['<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;">']
    for title, rows, cols in sections:
        text.append(title)
        html.append(f'<h3 style="{STYLE_H}">{escape(title)}</h3>')
        if cols:
            html.append(f'<table style="{STYLE_T}"><tr>'
                        + "".join(f'<th style="{STYLE_C}">{escape(c)}</th>' for c in cols) + "</tr>")
            for r in rows:
                text.append("- " + " | ".join("" if c is None else str(c) for c in r))
                html.append("<tr>" + "".join(f'<td style="{STYLE_C}">{escape("" if c is None else str(c))}</td>'
                                             for c in r) + "</tr>")
            html.append("</table>")
        else:
            html.append("<ul>")
            for r in rows:
                line = " ".join(str(c) for c in r if c is not None)
                text.append("- " + line)
                html.append(f'<li style="{STYLE_P}">{escape(line)}</li>')
            html.append("</ul>")
        text.append("")
    html.append("</div>")
    return {"subject": subject, "html": "".join(html), "text": "\n".join(text).rstrip() + "\n"}

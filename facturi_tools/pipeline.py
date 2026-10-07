"""The daily run: read the mailbox, file invoices, record statements, match payments, send a report.

Every collaborator is passed in, so the whole run can be tested with fakes. `run` never raises:
each failure is caught per message or per step, recorded in `errors` and the run goes on. A
message whose processing failed is left in the inbox, so the next run retries it. In `dryrun`
nothing is written anywhere (no upload, no ledger write, no move, no folder, no email, no ping);
data a write would have produced is kept in memory under a negative synthetic id.
"""
import datetime
import hashlib
import math
import os
import re
from collections import Counter

from facturi_tools import inspect_pdf, naming
from facturi_tools import statement as statement_parser

INBOX = "Inbox"
TOLERANCE = 0.005
REMINDER_DAYS = 7
MAX_REMINDERS = 4
MAX_TRANSIENT_STREAK = 3
MAX_UPLOAD_NAME = 120
TX_CHUNK = 20  # rows per insert call, so a failure part way leaves a repairable, partial set
DOC_TYPES = {"invoice", "credit_note", "receipt", "other"}
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _utc(now):
    if now is None:
        return datetime.datetime.now(datetime.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=datetime.timezone.utc)
    return now.astimezone(datetime.timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _f(value):
    """Number or None. NocoDB can hand decimals back as strings."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _cap(value, n=200):
    """Reader strings are untrusted: cut them before they reach a ledger row or the result."""
    return value[:n] if isinstance(value, str) else value


def _date_ok(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _drop_none(d):
    return {k: v for k, v in d.items() if v is not None}


def _parse_ts(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def _same_id(a, b):
    return a is not None and b is not None and str(a) == str(b)


def _empty(value):
    return value is None or value == "" or value == 0


class _Run:
    def __init__(self, cfg, mode, zoho, ledger, read, upload, now, ping):
        self.cfg = cfg
        self.mode = mode
        self.live = mode == "live"
        self.zoho = zoho
        self.ledger = ledger
        self.read = read
        self.upload = upload
        self.ping = ping
        self.now = _utc(now)
        self.now_iso = _iso(self.now)
        self.started = self.now_iso
        self.items = []
        self.errors = []
        self.stmts = []
        self.proposed = []
        self.gaps = []
        self.folders_info = {}
        self.counts = {"messages": 0, "filed": 0, "needs_review": 0, "duplicate": 0, "no_pdf": 0,
                       "statements": 0, "matched": 0, "missing": 0, "errors": 0}
        self._syn = 0
        self.suppliers = None
        self.mem_invoices = []   # invoice rows created in this run (live: with their real Id)
        self.mem_tx = []         # dryrun only: transaction rows a live run would have written
        self.mem_stmts = []      # dryrun only: statement rows a live run would have written
        self.seen_sha = {}
        self.seen_num = {}
        self.seen_stmt_sha = {}
        self.seen_stmt_period = {}
        self.stmt_entries = {}   # statement id (str) -> entry of result["statements"]
        self.stmt_no = {}        # statement id (str) -> statement number
        self.added_stmt_ids = set()
        self.tx_open = []
        self.invoices_known = []
        self.reader_stopped = False
        self.transient_streak = 0
        self.reopened = set()       # statement ids reopened by a repair in this run
        self.failed_stmts = set()   # statement ids whose transactions could not be recorded in this run
        self.required = {}          # statement id (str) -> rows its PDF requires
        self.done_id = None
        self.review_id = None

    # ---- small helpers -------------------------------------------------

    def synth(self):
        self._syn += 1
        return -self._syn

    def safe(self, exc):
        return self.scrub(f"{type(exc).__name__}: {exc}")[:300]

    def scrub(self, text):
        secrets = []
        for k, v in (self.cfg or {}).items():
            if isinstance(v, str) and len(v) >= 6 and (k == "HEARTBEAT_URL" or any(w in k.upper() for w in SECRET_WORDS)):
                secrets.append(v)
                if k == "HEARTBEAT_URL":
                    # a ping URL keeps its secret in the path or in the query: scrub each part on its own
                    rest = v.split("://", 1)[-1].partition("/")[2]
                    path, _, query = rest.partition("?")
                    parts = [path, query] + [q.partition("=")[2] for q in query.split("&")]
                    secrets.extend(x for x in parts if len(x) >= 6)
        for v in sorted(secrets, key=len, reverse=True):
            text = text.replace(v, "***")
        return text

    def err(self, where, exc_or_text):
        text = self.scrub(exc_or_text) if isinstance(exc_or_text, str) else self.safe(exc_or_text)
        self.errors.append({"where": self.scrub(str(where)), "message": text})

    def _setting(self, name, default):
        value = self.cfg.get(name)
        return default if value in (None, "") else value

    # ---- step 1 and 2: folders, messages --------------------------------

    def run_all(self):
        try:
            self._steps()
        except Exception as e:  # last resort: run() must never raise
            self.err("run", e)
        return self._result()

    def _steps(self):
        if self.mode not in ("dryrun", "live"):
            self.err("run", f"unknown mode {self.mode!r}")
            return
        try:
            self.zoho.refresh()
            folders = self.zoho.folders()
        except Exception as e:
            self.err("mailbox", e)
            return
        inbox = folders.get(INBOX)
        names = {"done": self._setting("FOLDER_DONE", "Procesate"), "review": self._setting("FOLDER_REVIEW", "De verificat")}
        self.names = names
        if self.live:
            try:
                self.done_id = self.zoho.ensure_folder(names["done"])
                self.review_id = self.zoho.ensure_folder(names["review"])
            except Exception as e:
                self.err("folders", e)
                return
        else:
            self.folders_info = {n: n in folders for n in names.values()}
        if not inbox:
            self.err("mailbox", "the inbox folder was not found")
            return
        try:
            self.suppliers = self.ledger.suppliers()
        except Exception as e:
            self.err("suppliers", e)
        try:
            for r in self.ledger.open_statements():
                self.stmt_no[str(r.get("Id"))] = r.get("statement_no")
        except Exception as e:
            self.err("statements", e)

        limit = int(self._setting("MAX_MESSAGES", 50))
        try:
            messages = self.zoho.list_folder(inbox, limit)
        except Exception as e:
            self.err("mailbox", e)
            return
        messages = sorted(messages, key=lambda m: _f(m.get("receivedTime")) or 0)[:limit]
        self.counts["messages"] = len(messages)
        for msg in messages:
            try:
                self._message(msg, inbox)
            except Exception as e:
                self.err(f"message {msg.get('messageId')}", e)
        self._match_step()
        self._statements_step()

    def _message(self, msg, inbox):
        mid = str(msg.get("messageId"))
        fid = str(msg.get("folderId") or inbox)
        base = {"messageId": mid, "from": msg.get("fromAddress") or msg.get("sender"),
                "subject": msg.get("subject")}
        try:
            atts = self.zoho.attachments(fid, mid)
        except Exception as e:
            self.err(f"attachments {mid}", e)
            self.items.append({**base, "attachment": None, "outcome": "error", "moveTo": None,
                               "note": "nu am putut citi atașamentele"})
            return
        pdfs, others = [], []
        for a in atts:
            n = str(a.get("attachmentName") or "").strip()
            # a name with no extension may still be a document: it is sniffed, not dropped
            (pdfs if n.lower().endswith(".pdf") or not os.path.splitext(n)[1] else others).append(a)
        other_names = [_cap(str(a.get("attachmentName") or ""), 120) for a in others]
        mine = []
        if not pdfs:
            self.counts["no_pdf"] += 1
            mine.append({**base, "attachment": None, "outcome": "no_pdf", "note": None})
        for a in pdfs:
            name = str(a.get("attachmentName"))
            try:
                item = self._pdf(msg, fid, mid, a, name)
            except Exception as e:
                self.err(f"{mid} {name}", e)
                item = {"attachment": name, "outcome": "error", "note": self.safe(e)}
            mine.append({**base, **item})
        if other_names:
            for i in mine:
                i["otherAttachments"] = other_names
        outcomes = {i["outcome"] for i in mine}
        if outcomes & {"error", "reader_unavailable"}:
            dest = None
        elif outcomes & {"needs_review", "no_pdf", "statement_unverified"}:
            dest = "review"
        else:
            dest = "done"
        dest_name = self.names[dest] if dest else None
        for i in mine:
            i["moveTo"] = dest_name
            self.items.append(i)
        if dest and self.live:
            try:
                self.zoho.move([mid], self.done_id if dest == "done" else self.review_id)
            except Exception as e:
                self.err(f"move {mid}", e)

    # ---- step 3: invoice ------------------------------------------------

    def _pdf(self, msg, fid, mid, att, name):
        data = self.zoho.download(fid, mid, att.get("attachmentId"))
        if not isinstance(data, (bytes, bytearray)) or not bytes(data[:4]) == b"%PDF":
            return self._unreadable(msg, bytes(data or b""), name, "nu este un PDF (gol sau alt tip de fișier)")
        try:
            info = inspect_pdf.inspect(data, name)
        except Exception:
            return self._unreadable(msg, bytes(data), name, "PDF ilizibil")
        if info.get("kind") == "statement":
            return self._statement(data, name, info)
        return self._invoice(msg, data, name, info)

    def _by_hash(self, sha):
        return self.seen_sha.get(sha) or self.ledger.find_invoice_by_hash(sha)

    def _by_number(self, short, number):
        return self.seen_num.get((short, number)) or self.ledger.find_invoice_by_number(short, number)

    def _canonical(self, reader):
        """The reader's casing of supplier_short varies between calls. When a supplier row owns the
        invoice, its supplier_short is used for the file name, the ledger row and the number lookup."""
        if not self.suppliers or not reader.get("supplier_short"):
            return reader
        from facturi_tools import matcher
        active = [x for x in self.suppliers if x.get("status") == "active"]
        owner = matcher.owner_of({"supplier_short": reader.get("supplier_short"),
                                  "supplier_name": reader.get("supplier_name")}, active)
        return {**reader, "supplier_short": owner} if owner else reader

    @staticmethod
    def _reader_kind(exc):
        kind = getattr(exc, "kind", None)
        if kind in ("transient", "config", "document"):
            return kind
        return "transient" if getattr(exc, "transient", False) else "document"

    def _unreadable(self, msg, data, name, reason):
        """A document that can never be read: filed for review so it does not block the mailbox."""
        sha = hashlib.sha256(data).hexdigest()
        item = {"attachment": name, "note": None}
        if self._by_hash(sha):
            self.counts["duplicate"] += 1
            return {**item, "outcome": "duplicate", "note": "același fișier există deja în registru"}
        plan = naming.plan_invoice({}, sha, name)
        note = "nu am putut citi documentul: " + reason
        row = _drop_none({"file_name": plan["fileName"], "onedrive_path": plan["path"], "sha256": sha,
                          "source": "email", "mail_message_id": str(msg.get("messageId")),
                          "received_at": self._received(msg), "status": "needs_review", "notes": note})
        item.update(path=plan["path"], note=note)
        if self.live:
            self.upload(plan["path"], data)
            row["Id"] = (self.ledger.add_invoice(row) or {}).get("Id")
            item["ledgerId"] = row["Id"]
        else:
            row["Id"] = self.synth()
            item.update(ledgerId=row["Id"], synthetic=True)
        self.mem_invoices.append(row)
        self.seen_sha[sha] = row
        self.counts["needs_review"] += 1
        return {**item, "outcome": "needs_review"}

    def _invoice(self, msg, data, name, info):
        sha = info["sha256"]
        item = {"attachment": name, "note": None}
        if self._by_hash(sha):
            self.counts["duplicate"] += 1
            return {**item, "outcome": "duplicate", "note": "același fișier există deja în registru"}
        if self.reader_stopped:
            return {**item, "outcome": "reader_unavailable", "note": "cititorul nu este disponibil în această rulare"}
        try:
            reader = self.read(data)
        except Exception as e:
            kind = self._reader_kind(e)
            if kind == "config":
                # a revoked key or a wrong model must not turn invoices into review rows
                self.reader_stopped = True
                self.err("reader", "the reader is not usable (check its key, permission and model); "
                                   "invoice mails stay in the inbox until it works")
                return {**item, "outcome": "reader_unavailable", "note": "cititorul nu este utilizabil"}
            if kind == "transient":
                self.transient_streak += 1
                if self.transient_streak >= MAX_TRANSIENT_STREAK:
                    self.reader_stopped = True
                    self.err("reader", "the reader failed three times in a row, not called again in this run")
                raise  # network, 429 or 5xx: the mail stays for a retry
            self.transient_streak = 0
            return self._unreadable(msg, data, name, "cititorul a refuzat documentul")
        self.transient_streak = 0
        if not isinstance(reader, dict):
            reader = {}
        reader = self._canonical(reader)
        plan = naming.plan_invoice(reader, sha, name, buyer_cui=self.cfg.get("FACTURI_BUYER_CUI") or "")
        short = _cap(reader.get("supplier_short"))
        number = _cap(reader.get("invoice_number"))
        item.update(path=plan["path"], supplier=short, invoiceNumber=number, total=_f(reader.get("total")),
                    currency=_cap(reader.get("currency"), 10), issueDate=_cap(reader.get("issue_date"), 10),
                    buyerVatOk=plan["buyerVatOk"], buyerVatId=_cap(reader.get("buyer_vat_id"), 60))
        if plan["status"] != "needs_review" and short and number:
            twin = self._by_number(str(short), str(number))
            # a needs_review twin is an incomplete earlier copy: the complete file is still filed
            if twin and twin.get("status") != "needs_review":
                self.counts["duplicate"] += 1
                return {**item, "outcome": "duplicate", "path": None,
                        "note": "același număr de factură există deja în registru"}
        review = plan["status"] == "needs_review"
        row = self._invoice_row(reader, plan, sha, msg, review)
        if self.live:
            self.upload(plan["path"], data)
            added = self.ledger.add_invoice(row)
            row["Id"] = (added or {}).get("Id")
            item["ledgerId"] = row["Id"]
        else:
            row["Id"] = self.synth()
            item.update(ledgerId=row["Id"], synthetic=True)
        self.mem_invoices.append(row)
        self.seen_sha[sha] = row
        if short and number:
            self.seen_num[(str(short), str(number))] = row
        if review:
            self.counts["needs_review"] += 1
            item["note"] = "lipsește: " + ", ".join(plan["missing"])
            return {**item, "outcome": "needs_review"}
        self.counts["filed"] += 1
        return {**item, "outcome": "filed"}

    def _invoice_row(self, reader, plan, sha, msg, review):
        doc = reader.get("doc_type")
        total = _f(reader.get("total"))
        row = {
            "doc_type": doc if doc in DOC_TYPES else None,
            "supplier_short": _cap(reader.get("supplier_short"), 200) or None,
            "supplier_name": _cap(reader.get("supplier_name"), 200) or None,
            "invoice_number": _cap(reader.get("invoice_number"), 200) or None,
            "issue_date": reader.get("issue_date") if _date_ok(reader.get("issue_date")) else None,
            "total": total,
            "currency": _cap(reader.get("currency"), 10) or None,
            "vat_amount": _f(reader.get("vat_amount")),
            "buyer_vat_id": _cap(reader.get("buyer_vat_id"), 60) or None,
            "buyer_vat_ok": plan["buyerVatOk"],
            "payment_reference": _cap(reader.get("payment_reference"), 200) or None,
            "period_start": reader.get("period_start") if _date_ok(reader.get("period_start")) else None,
            "period_end": reader.get("period_end") if _date_ok(reader.get("period_end")) else None,
            "file_name": plan["fileName"],
            "onedrive_path": plan["path"],
            "sha256": sha,
            "source": "email",
            "mail_message_id": str(msg.get("messageId")),
            "received_at": self._received(msg),
            "status": "needs_review" if review else "filed",
            "notes": ("lipsește: " + ", ".join(plan["missing"])) if review else (_cap(reader.get("notes"), 1000) or None),
        }
        return _drop_none(row)

    def _received(self, msg):
        ms = _f(msg.get("receivedTime"))
        if ms is None:
            return None
        return _iso(datetime.datetime.fromtimestamp(ms / 1000.0, datetime.timezone.utc))

    # ---- step 4: statement ----------------------------------------------

    def _statement(self, data, name, info):
        sha = info["sha256"]
        item = {"attachment": name, "note": None}
        self.counts["statements"] += 1
        if self.suppliers is None:
            raise RuntimeError("the supplier list could not be read, statement not processed")
        sid = self.seen_stmt_sha.get(sha)
        hit = None
        if sid is None:
            hit = self.ledger.find_statement_by_hash(sha)
            sid = (hit or {}).get("Id") if hit else None
        st = statement_parser.parse_statement(data)
        end = st.get("periodEnd")
        period = end[:7] if _date_ok(end) else None
        if sid is not None:
            # the row exists, but its transactions may not all be there (an earlier run failed half way)
            item["ledgerId"] = sid
            if not st.get("verified"):
                return {**item, "outcome": "statement_known", "note": "extrasul a fost deja procesat"}
            self.stmt_no[str(sid)] = st.get("statementNo")
            added = self._ensure_rows(sid, st, period, entry_outcome="statement_repaired", hit=hit)
            if added:
                note = f"rânduri lipsă adăugate: {added}"
                if str(sid) in self.reopened:
                    note += "; extrasul era închis și a fost redeschis"
                return {**item, "outcome": "statement_repaired", "note": note}
            return {**item, "outcome": "statement_known", "note": "extrasul a fost deja procesat"}
        if st.get("verified") and st.get("statementNo") and st.get("periodStart") and end:
            twin = self.seen_stmt_period.get((str(st["statementNo"]), st["periodStart"], end)) or \
                self.ledger.find_statement_by_period(st["statementNo"], st["periodStart"], end)
            if twin:
                return {**item, "outcome": "statement_known",
                        "note": "același număr și aceeași perioadă ca un extras deja înregistrat"}
        year = end[:4] if _date_ok(end) else "necunoscut"
        path = f"Extrase/{year}/{self._file_name(name, sha)}"
        item["path"] = path
        if self.live:
            self.upload(path, data)
        if not st.get("verified"):
            entry = {"statementNo": st.get("statementNo"), "period": period, "verified": False, "checked": 0,
                     "open": 0, "outcome": "statement_unverified", "added": False, "complete": False,
                     "reminder": False, "verifyError": st.get("verifyError")}
            self.stmts.append(entry)
            return {**item, "outcome": "statement_unverified", "note": st.get("verifyError")}
        checked, expected = self._wanted(st)
        srow = _drop_none({
            "statement_no": st.get("statementNo"), "period_start": st.get("periodStart"),
            "period_end": end, "sha256": sha, "n_debits": st.get("nDebits"), "n_credits": st.get("nCredits"),
            "total_debits": st.get("totalDebits"), "total_credits": st.get("totalCredits"),
            "verified": True, "open_gaps": len(checked) + len(expected), "reminders_sent": 0,
            # the owner is told about the statement in this run's report: the first reminder is 7 days away
            "last_reminder_at": self.now_iso, "rows_required": len(checked) + len(expected)})
        if self.live:
            sid = (self.ledger.add_statement(srow) or {}).get("Id")
        else:
            sid = self.synth()
            item["synthetic"] = True
            self.mem_stmts.append({**srow, "Id": sid})
        item["ledgerId"] = sid
        self.seen_stmt_sha[sha] = sid
        if st.get("statementNo") and st.get("periodStart") and end:
            self.seen_stmt_period[(str(st["statementNo"]), st["periodStart"], end)] = sid
        self.stmt_no[str(sid)] = st.get("statementNo")
        self.added_stmt_ids.add(str(sid))
        entry = {"statementNo": st.get("statementNo"), "period": period, "verified": True,
                 "checked": len(checked), "open": 0, "outcome": "statement_added", "added": True,
                 "complete": False, "reminder": False, "statementId": sid}
        if not self.live:
            entry["synthetic"] = True
        self.stmts.append(entry)
        self.stmt_entries[str(sid)] = entry
        self._ensure_rows(sid, st, period)
        return {**item, "outcome": "statement_added"}

    def _wanted(self, st):
        checked = [t for t in st.get("transactions", []) if self._is_checked(t)]
        expected = [s for s in self.suppliers
                    if s.get("rule") == "expected_no_payment" and s.get("status") == "active"]
        return checked, expected

    @staticmethod
    def _row_key(row):
        if row.get("kind") == "expected":
            return ("expected", str(row.get("counterparty") or ""), str(row.get("proc_date") or "")[:10])
        return ("tx", str(row.get("bank_ref") or ""), str(row.get("proc_date") or "")[:10],
                round(_f(row.get("amount_ron")) or 0.0, 2))

    def _ensure_rows(self, sid, st, period, entry_outcome=None, hit=None):
        """Add the transaction rows the statement requires and the ledger lacks. Returns how many were added.

        Runs whenever the statement's PDF is seen, so a run that failed half way is repaired by the next.
        On failure the statement is marked failed for this run (never completed) and the error propagates,
        which leaves the mail in the inbox.
        """
        key = str(sid)
        end = st.get("periodEnd")
        checked, expected = self._wanted(st)
        rows = [self._tx_row(sid, t) for t in checked]
        for sup in expected:
            rows.append(_drop_none({
                "statement": sid, "proc_date": end, "counterparty": f"(expected) {sup.get('supplier_short')}",
                "kind": "expected", "amount_ron": 0, "status": "missing", "reason": "expected_no_payment"}))
        try:
            real = not key.startswith("-")
            have = list(self.ledger.transactions_for_statement(sid)) if real else []
            if not self.live:
                have += [r for r in self.mem_tx if _same_id(r.get("statement"), sid)]
            counts = Counter(self._row_key(r) for r in have)
            missing = []
            for r in rows:
                k = self._row_key(r)
                if counts[k] > 0:
                    counts[k] -= 1
                else:
                    missing.append(r)
            for i in range(0, len(missing), TX_CHUNK):
                chunk = missing[i:i + TX_CHUNK]
                if self.live:
                    self.ledger.add_transactions(chunk)
                else:
                    self.mem_tx.extend({**r, "Id": self.synth()} for r in chunk)
            if hit is not None:
                fields = {}
                if missing and hit.get("completed_at"):
                    fields["completed_at"] = None   # rows were missing: the statement is open again
                if _f(hit.get("rows_required")) is None:
                    fields["rows_required"] = len(rows)   # old row: it is repaired first, then it may complete
                if fields:
                    if self.live:
                        self.ledger.update_statement(sid, fields)
                    elif "completed_at" in fields:
                        self.mem_stmts.append({**hit, "completed_at": None})
                    if "completed_at" in fields:
                        self.reopened.add(key)
        except Exception:
            self.failed_stmts.add(key)
            entry = self.stmt_entries.get(key)
            if entry:
                entry.update(added=False, failed=True, outcome="statement_error")
            raise
        self.required[key] = len(rows)
        if missing and entry_outcome and key not in self.stmt_entries:
            entry = {"statementNo": st.get("statementNo"), "period": period, "verified": True,
                     "checked": len(checked), "open": 0, "outcome": entry_outcome, "added": True,
                     "complete": False, "reminder": False, "statementId": sid, "reopened": key in self.reopened}
            self.stmts.append(entry)
            self.stmt_entries[key] = entry
            self.added_stmt_ids.add(key)
        return len(missing)

    @staticmethod
    def _file_name(name, sha):
        """Sanitized like invoice names and at most 120 characters. Deterministic, so a retry overwrites."""
        clean = naming.sanitize(name)
        if not clean.lower().endswith(".pdf"):
            clean = (clean or f"extras-{sha[:8]}") + ".pdf"
        if len(clean) > MAX_UPLOAD_NAME:
            stem = clean[:-4][:MAX_UPLOAD_NAME - 4 - 9].rstrip("-.")
            clean = f"{stem}_{sha[:8]}.pdf"
        return clean

    @staticmethod
    def _is_checked(t):
        amount = _f(t.get("amountRon"))
        if t.get("kind") == "card":
            return amount is not None and amount < 0
        if t.get("kind") == "transfer":
            return bool(t.get("ibanCountry")) and t.get("ibanCountry") != "RO"
        return False

    @staticmethod
    def _tx_row(sid, t):
        return _drop_none({
            "statement": sid, "proc_date": t.get("procDate"), "card_date": t.get("cardDate"),
            "bank_ref": t.get("bankRef"), "counterparty": t.get("counterparty"),
            "amount_ron": _f(t.get("amountRon")), "orig_amount": _f(t.get("origAmount")),
            "orig_currency": t.get("origCurrency"), "status": "missing", "kind": t.get("kind"),
            "iban_country": t.get("ibanCountry"), "descriptor_ref": t.get("descriptorRef")})

    # ---- step 5: matching -----------------------------------------------

    @staticmethod
    def _tx_for_matcher(row):
        return {"id": row.get("Id"), "index": None, "procDate": row.get("proc_date"),
                "cardDate": row.get("card_date"), "bankRef": row.get("bank_ref"),
                "counterparty": row.get("counterparty"), "kind": row.get("kind"),
                "amountRon": row.get("amount_ron"), "origAmount": row.get("orig_amount"),
                "origCurrency": row.get("orig_currency"), "ibanCountry": row.get("iban_country"),
                "descriptorRef": row.get("descriptor_ref")}

    @staticmethod
    def _inv_for_matcher(row):
        return {"id": row.get("Id"), "supplier_short": row.get("supplier_short"),
                "supplier_name": row.get("supplier_name"), "invoice_number": row.get("invoice_number"),
                "issue_date": row.get("issue_date"), "total": _f(row.get("total")),
                "currency": row.get("currency"), "payment_reference": row.get("payment_reference"),
                "matched": not _empty(row.get("matched_tx"))}

    def _write(self, fn, *args, where):
        """Run a ledger write in live mode. Returns True when the change counts (dryrun: always)."""
        if not self.live:
            return True
        try:
            fn(*args)
            return True
        except Exception as e:
            self.err(where, e)
            return False

    def _match_step(self):
        if self.suppliers is None:
            return
        try:
            self._match()
        except Exception as e:
            self.err("match", e)

    def _match(self):
        import copy
        from facturi_tools import matcher

        rows = copy.deepcopy(self.ledger.open_transactions())
        if not self.live:
            rows += self.mem_tx
        self.tx_open = rows
        unmatched = copy.deepcopy(self.ledger.unmatched_invoices())
        if not self.live:
            # planned invoices exist only in memory; a live run reads them back from the ledger
            unmatched += [r for r in self.mem_invoices if r.get("status") == "filed"]
        by_id = {str(r.get("Id")): r for r in unmatched}
        self.invoices_known = list(unmatched) + [r for r in self.mem_invoices
                                                 if str(r.get("Id")) not in by_id and r.get("status") == "filed"]
        # live: rows added in this run are already in unmatched (read back from the ledger)
        self._complete_pointed([r for r in rows if r.get("kind") != "expected"])
        normal = [r for r in rows if r.get("kind") != "expected" and r.get("status") == "missing"]
        if normal:
            out = matcher.match([self._tx_for_matcher(r) for r in normal],
                                [self._inv_for_matcher(r) for r in unmatched], self.suppliers, None)
            self._apply(normal, out, by_id)
            self._propose(out.get("proposedSuppliers") or [])
        self._close_expected([r for r in rows if r.get("kind") == "expected"])

    def _complete_pointed(self, rows):
        """Repair a run that failed between the invoice write and the transaction write.

        A match is written invoice first, so an open transaction that an invoice already points at is
        a finished match whose second write was lost.
        """
        for row in rows:
            tid = row.get("Id")
            if tid is None or str(tid).startswith("-"):
                continue
            try:
                inv = self.ledger.find_invoice_by_matched_tx(tid)
            except Exception as e:
                self.err(f"transaction {tid}", e)
                continue
            if not inv:
                continue
            ref, pref = str(row.get("descriptor_ref") or "").lower(), str(inv.get("payment_reference") or "").lower()
            fields = {"status": "matched", "reason": "reference" if ref and ref == pref else "exact",
                      "matched_invoice": inv.get("Id")}
            if self._write(self.ledger.update_transaction, tid, fields, where=f"transaction {tid}"):
                row.update(fields)
                self.counts["matched"] += 1

    def _apply(self, normal, out, by_id):
        for res in out.get("results", []):
            row = normal[res["txPos"]]
            status, reason = res.get("status"), res.get("reason")
            tid = row.get("Id")
            if status == "matched":
                fields = {"status": "matched", "reason": reason, "matched_invoice": res.get("invoiceId")}
                if reason in ("exact", "reference") and res.get("invoiceId") is not None:
                    # invoice first: a failure between the two writes then shows as a gap (safe) and the
                    # next run completes the pair, never as a covered payment with a reusable invoice
                    inv = by_id.get(str(res["invoiceId"]))
                    if not self._write(self.ledger.update_invoice, res["invoiceId"], {"matched_tx": tid},
                                       where=f"invoice {res['invoiceId']}"):
                        continue
                    if inv is not None:
                        inv["matched_tx"] = tid
                if self._write(self.ledger.update_transaction, tid, fields, where=f"transaction {tid}"):
                    row.update(fields)
                    self.counts["matched"] += 1
            elif status == "skipped":
                fields = {"status": "skipped", "reason": reason}
                if self._write(self.ledger.update_transaction, tid, fields, where=f"transaction {tid}"):
                    row.update(fields)
            elif status == "missing":
                cand = res.get("candidateInvoiceId")
                fields = {}
                if row.get("reason") != reason:
                    fields["reason"] = reason
                if (None if _empty(row.get("candidate_invoice")) else str(row.get("candidate_invoice"))) != \
                        (None if cand is None else str(cand)):
                    fields["candidate_invoice"] = cand
                if fields and self._write(self.ledger.update_transaction, tid, fields, where=f"transaction {tid}"):
                    row.update(fields)

    def _propose(self, proposed):
        have = {str(s.get("descriptor_pattern") or "").lower() for s in self.suppliers}
        for p in proposed:
            pattern = str(p.get("descriptor_pattern") or "")
            if not pattern or pattern.lower() in have:
                continue
            row = {**p, "status": "proposed"}
            if self._write(self.ledger.add_supplier, row, where=f"supplier {pattern}"):
                have.add(pattern.lower())
                self.suppliers.append(row)
                self.proposed.append({k: p.get(k) for k in ("supplier_short", "descriptor_pattern",
                                                            "invoice_match", "rule")})

    def _close_expected(self, expected_rows):
        if not expected_rows:
            return
        from facturi_tools import matcher
        active = [s for s in self.suppliers if s.get("status") == "active"]
        for row in expected_rows:
            short = str(row.get("counterparty") or "").replace("(expected) ", "", 1)
            month = str(row.get("proc_date") or "")[:7]
            for inv in self.invoices_known:
                if str(inv.get("issue_date") or "")[:7] != month:
                    continue
                if matcher.owner_of(self._inv_for_matcher(inv), active) != short:
                    continue
                fields = {"status": "matched", "reason": "expected_no_payment", "matched_invoice": inv.get("Id")}
                tid = row.get("Id")
                if self._write(self.ledger.update_transaction, tid, fields, where=f"transaction {tid}"):
                    row.update(fields)
                break

    # ---- step 6: statements ---------------------------------------------

    def _statements_step(self):
        try:
            self._statements()
        except Exception as e:
            self.err("statements", e)

    def _open_count(self, sid):
        if self.live:
            return int(self.ledger.count_open_for_statement(sid))
        return sum(1 for r in self.tx_open if r.get("status") == "missing" and _same_id(r.get("statement"), sid))

    def _statements(self):
        rows = self.ledger.open_statements()
        if not self.live:
            rows = list(rows) + [r for r in self.mem_stmts if _empty(r.get("completed_at"))]
        for r in rows:
            sid = r.get("Id")
            key = str(sid)
            self.stmt_no[key] = r.get("statement_no")
            if key in self.failed_stmts:
                continue  # its transactions are not all recorded: never complete, never remind
            try:
                n = self._open_count(sid)
            except Exception as e:
                self.err(f"statement {sid}", e)
                continue
            entry = self.stmt_entries.get(key)
            if entry:
                entry["open"] = n
            period = str(r.get("period_end") or "")[:7] or None
            if n == 0:
                if not self._fully_recorded(sid, key, r):
                    continue
                fields = {"completed_at": self.now_iso, "open_gaps": 0}
                if not self._write(self.ledger.update_statement, sid, fields, where=f"statement {sid}"):
                    continue
                if not entry:
                    entry = self._new_entry(r, sid, period)
                entry["complete"] = True
                if entry["outcome"] != "statement_added":
                    entry["outcome"] = "month_complete"
                continue
            fields = {}
            sent = int(_f(r.get("reminders_sent")) or 0)
            last = _parse_ts(r.get("last_reminder_at"))
            due = last is None or (self.now - last) >= datetime.timedelta(days=REMINDER_DAYS)
            if last is None:
                # legacy row, or one that was never stamped: start its clock now, remind later
                fields = {"last_reminder_at": self.now_iso}
                if _f(r.get("open_gaps")) != float(n):
                    fields["open_gaps"] = n
            elif key not in self.added_stmt_ids and sent < MAX_REMINDERS and due:
                fields = {"reminders_sent": sent + 1, "last_reminder_at": self.now_iso, "open_gaps": n}
            elif _f(r.get("open_gaps")) != float(n):
                fields = {"open_gaps": n}
            if fields and not self._write(self.ledger.update_statement, sid, fields, where=f"statement {sid}"):
                continue
            if "reminders_sent" in fields:
                if not entry:
                    entry = self._new_entry(r, sid, period)
                    entry["open"] = n
                entry["reminder"] = True
                entry["reminderNo"] = sent + 1
                if entry["outcome"] != "statement_added":
                    entry["outcome"] = "reminder"

    def _fully_recorded(self, sid, key, row):
        """Complete only when the ledger holds at least the rows stored in the statement's own
        `rows_required`. A row without it is repaired first, when its PDF is next seen."""
        if str(sid).startswith("-"):
            return True
        need = _f(row.get("rows_required"))
        if need is None:
            need = self.required.get(key)
        if need is None:
            self.err(f"statement {sid}", "the statement row has no rows_required, not completed until its PDF is seen again")
            return False
        try:
            have = len(self.ledger.transactions_for_statement(sid))
        except Exception as e:
            self.err(f"statement {sid}", e)
            return False
        if not self.live:
            have += sum(1 for r in self.mem_tx if _same_id(r.get("statement"), sid))
        if have < need:
            self.err(f"statement {sid}", "the statement has fewer transaction rows than it requires, not completed")
            return False
        return True

    def _new_entry(self, row, sid, period):
        entry = {"statementNo": row.get("statement_no"), "period": period, "verified": True, "checked": None,
                 "open": 0, "outcome": "month_complete", "added": False, "complete": False,
                 "reminder": False, "statementId": sid}
        self.stmts.append(entry)
        self.stmt_entries[str(sid)] = entry
        return entry

    # ---- step 7, 8 and result -------------------------------------------

    def _build_gaps(self):
        invoices = {str(r.get("Id")): r for r in self.invoices_known}
        out = []
        for r in self.tx_open:
            if r.get("status") != "missing":
                continue
            orig = _f(r.get("orig_amount"))
            amount = orig if orig is not None else abs(_f(r.get("amount_ron")) or 0.0)
            currency = r.get("orig_currency") if orig is not None else "RON"
            cand = r.get("candidate_invoice")
            cand_inv = invoices.get(str(cand)) if not _empty(cand) else None
            candidate = None if _empty(cand) else ((cand_inv or {}).get("file_name") or str(cand))
            out.append({"kind": r.get("kind"), "procDate": r.get("proc_date"), "counterparty": r.get("counterparty"),
                        "amount": round(amount, 2), "currency": currency or "RON",
                        "reason": r.get("reason") or "no_invoice", "candidate": candidate,
                        "statement": self.stmt_no.get(str(r.get("statement")))})
        return out

    def _result(self):
        self.gaps = self._build_gaps()
        self.counts["missing"] = len(self.gaps)
        result = {"mode": self.mode, "startedAt": self.started, "finishedAt": None, "ok": True,
                  "counts": self.counts, "items": self.items, "gaps": self.gaps, "statements": self.stmts,
                  "proposedSuppliers": self.proposed, "errors": self.errors, "email": None}
        if not self.live:
            result["folders"] = self.folders_info
            result["syntheticIds"] = "negative ids exist only in this dry run, they are not ledger ids"
        self._report(result)
        self._heartbeat()
        self.counts["errors"] = len(self.errors)
        result["ok"] = not self.errors
        result["finishedAt"] = _iso(_utc(None) if self.live else self.now)
        return result

    def _report(self, result):
        from facturi_tools import report
        try:
            self.counts["errors"] = len(self.errors)
            mail = report.build_email(result)
        except Exception as e:
            self.err("report", e)
            return
        if not mail:
            return
        result["email"] = {"subject": mail["subject"], "text": mail["text"]}
        if self.live:
            to = self.cfg.get("REPORT_TO")
            if not to:
                self.err("report", "REPORT_TO is not set, the report was not sent")
                return
            try:
                self.zoho.send(to, mail["subject"], mail["html"])
            except Exception as e:
                self.err("report", e)

    def _heartbeat(self):
        url = self.cfg.get("HEARTBEAT_URL")
        if not (self.live and url and not self.errors):
            return
        try:
            ping = self.ping
            if ping is None:
                from facturi_tools import httpx
                ping = lambda u: httpx.request("GET", u, timeout=20)[0]  # noqa: E731
            status = ping(url)
            if not isinstance(status, int) or status >= 400:
                self.err("heartbeat", f"the heartbeat URL answered {status}")
        except Exception as e:
            self.err("heartbeat", e)


def run(cfg, mode, zoho, ledger, read, upload, now=None, ping=None):
    """Run the pipeline. `ping(url)` returns an HTTP status and is injectable for tests."""
    try:
        return _Run(cfg, mode, zoho, ledger, read, upload, now, ping).run_all()
    except Exception as e:  # never raise, whatever happened
        return {"mode": mode, "startedAt": None, "finishedAt": None, "ok": False,
                "counts": {"messages": 0, "filed": 0, "needs_review": 0, "duplicate": 0, "no_pdf": 0,
                           "statements": 0, "matched": 0, "missing": 0, "errors": 1},
                "items": [], "gaps": [], "statements": [], "proposedSuppliers": [],
                "errors": [{"where": "run", "message": f"{type(e).__name__}: {str(e)[:200]}"}], "email": None}

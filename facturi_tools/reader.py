"""Invoice reader: one supplier PDF in, the reader-contract JSON out, via the Gemini API.

Moved from the command-line script without changing behaviour. The instruction text lives in
`reader_instruction.txt` next to this file, byte for byte as proven on the real documents; a
test pins its SHA-256. The request body has no key in it: the key travels only in the
x-goog-api-key header, and no error raised here contains it.
"""
import base64
import json
import re
from pathlib import Path

from facturi_tools import httpx

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"

KEYS = ["doc_type", "supplier_name", "supplier_short", "invoice_number", "issue_date",
        "total", "currency", "vat_amount", "buyer_vat_id", "payment_reference",
        "period_start", "period_end", "notes"]

SYSTEM_INSTRUCTION = (Path(__file__).with_name("reader_instruction.txt")).read_bytes().decode("utf-8")

USER_TEXT = "Extract the fields from this document."


def _nullable(t, **extra):
    d = {"type": [t, "null"]}
    d.update(extra)
    return d


# Every key is required so the model must answer each one; null is allowed where the
# contract allows it. Key order here is the order the model writes them.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "doc_type": {"type": "string", "enum": ["invoice", "credit_note", "receipt", "other"]},
        "supplier_name": _nullable("string"),
        "supplier_short": _nullable("string"),
        "invoice_number": _nullable("string"),
        "issue_date": _nullable("string", description="YYYY-MM-DD"),
        "total": _nullable("number"),
        "currency": _nullable("string", description="ISO 4217 code"),
        "vat_amount": _nullable("number"),
        "buyer_vat_id": _nullable("string"),
        "payment_reference": _nullable("string"),
        "period_start": _nullable("string", description="YYYY-MM-DD"),
        "period_end": _nullable("string", description="YYYY-MM-DD"),
        "notes": _nullable("string"),
    },
    "required": KEYS,
}


class ReaderError(Exception):
    """The reader could not produce a usable answer. The message never contains the key.

    kind: "transient" (network failure, HTTP 429, 5xx: try again later), "config" (HTTP 401, 403,
    404 or any other unexpected status such as a redirect: key, model or address is wrong) or
    "document" (everything else: this document cannot be read).
    """

    def __init__(self, message, status=None, details=None, kind="document"):
        super().__init__(message)
        self.status = status
        self.details = details
        self.kind = kind

    @property
    def transient(self):
        return self.kind == "transient"


def _kind_for(status):
    if status == 429 or status >= 500:
        return "transient"
    if status in (401, 403, 404) or status < 200 or 300 <= status < 400:
        return "config"
    return "document"


def build_request(pdf_bytes):
    """The JSON body for models/{model}:generateContent. No key in it."""
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{
            "role": "user",
            "parts": [
                {"inlineData": {"mimeType": "application/pdf",
                                "data": base64.b64encode(pdf_bytes).decode("ascii")}},
                {"text": USER_TEXT},
            ],
        }],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseJsonSchema": RESPONSE_SCHEMA,
        },
    }


def _strip_fence(text):
    t = text.strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n?(.*?)\n?```$", t, re.S)
    return m.group(1).strip() if m else t


def parse_response(resp):
    """Turn a generateContent response into the reader-contract dict."""
    if not isinstance(resp, dict):
        raise ReaderError("response is not a JSON object")
    block = (resp.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise ReaderError(f"prompt blocked: {block}")
    cands = resp.get("candidates") or []
    if not cands:
        raise ReaderError("empty response: no candidates")
    cand = cands[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and not p.get("thought"))
    finish = cand.get("finishReason")
    if not text.strip():
        raise ReaderError(f"empty answer (finishReason={finish})")
    try:
        data = json.loads(_strip_fence(text))
    except ValueError:
        raise ReaderError(f"answer is not JSON (finishReason={finish})")
    if not isinstance(data, dict):
        raise ReaderError("answer JSON is not an object")
    out = {k: data.get(k) for k in KEYS}
    if isinstance(out["total"], str):
        try:
            out["total"] = float(out["total"])
        except ValueError:
            pass
    return out


def _http_error(status, text):
    """Status plus Google's own error fields. Nothing from the request."""
    err = {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("error"), dict):
            err = parsed["error"]
    except ValueError:
        pass
    msg = f"HTTP {status} {err.get('status', '')}: {err.get('message', '')}".strip()
    return ReaderError(msg, status=status, details=err.get("details"), kind=_kind_for(status))


def read_invoice(pdf_bytes, key, model=DEFAULT_MODEL, timeout=180, http=httpx.request, meta=None):
    """meta, when given, receives rate-limit style headers and usageMetadata (none are secrets)."""
    try:
        status, headers, raw = http(
            "POST", f"{API_BASE}/models/{model}:generateContent",
            headers={"x-goog-api-key": key}, json_body=build_request(pdf_bytes), timeout=timeout)
    except httpx.NetworkError as e:
        raise ReaderError(f"unreachable: {e}".replace(key, "***") if key else f"unreachable: {e}",
                          kind="transient")
    text = raw.decode("utf-8", errors="replace")
    if not 200 <= status < 300:  # a redirect is not an answer: the address is wrong
        e = _http_error(status, text)
        raise ReaderError(str(e).replace(key, "***") if key else str(e), status=status, details=e.details,
                          kind=e.kind)
    try:
        resp = json.loads(text)
    except ValueError:
        raise ReaderError(f"answer is not JSON (HTTP {status})", status=status)
    if meta is not None:
        meta["headers"] = {k: v for k, v in (headers or {}).items()
                           if any(w in k.lower() for w in ("ratelimit", "quota", "retry"))}
        meta["usage"] = resp.get("usageMetadata") if isinstance(resp, dict) else None
    return parse_response(resp)

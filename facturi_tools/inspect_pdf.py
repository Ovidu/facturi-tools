"""Cheap, deterministic look at a PDF: hash, page count, and whether it is a bank statement."""
import hashlib
import re

_NAME_RE = re.compile(r"^ING_Extras_de_cont_.*\.pdf$", re.IGNORECASE)


def inspect(pdf_bytes, filename=None):
    import pymupdf
    pages, first = 0, None
    try:
        doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            pages = doc.page_count
            if pages:
                for ln in doc[0].get_text().split("\n"):
                    if ln.strip():
                        first = ln.strip()
                        break
        finally:
            doc.close()
    except Exception:
        pass
    by_name = bool(filename and _NAME_RE.match(filename))
    kind = "statement" if by_name or first == "Extras de cont" else "document"
    return {"sha256": hashlib.sha256(pdf_bytes).hexdigest(), "pages": pages,
            "kind": kind, "firstLine": first}

"""Settings the service reads from the environment. Names only live here, never values."""
import os
from pathlib import Path

DEFAULTS = {
    "ZOHO_DC": "eu",
    "GEMINI_MODEL": "gemini-3.8-flash",
    "FOLDER_DONE": "Procesate",
    "FOLDER_REVIEW": "De verificat",
}

STRINGS = [
    "FACTURI_KEY", "FACTURI_REMOTE", "FACTURI_BUYER_CUI", "FACTURI_ADMIN", "PORT", "RCLONE_CONFIG",
    "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN", "ZOHO_DC",
    "NOCODB_URL", "NOCODB_TOKEN", "NOCODB_TABLE_INVOICES", "NOCODB_TABLE_SUPPLIERS",
    "NOCODB_TABLE_TRANSACTIONS", "NOCODB_TABLE_STATEMENTS",
    "GEMINI_API_KEY", "GEMINI_MODEL", "REPORT_TO", "REPORT_FROM", "HEARTBEAT_URL",
    "FOLDER_DONE", "FOLDER_REVIEW",
]
DEFAULT_MAX_MESSAGES = 50

# Settings a run cannot start without.
REQUIRED_FOR_RUN = [
    "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN",
    "NOCODB_URL", "NOCODB_TOKEN", "NOCODB_TABLE_INVOICES", "NOCODB_TABLE_SUPPLIERS",
    "NOCODB_TABLE_TRANSACTIONS", "NOCODB_TABLE_STATEMENTS",
    "GEMINI_API_KEY", "FACTURI_REMOTE", "REPORT_TO",
]


def _read_key_file(value):
    try:
        p = Path(value).expanduser()
        return p.read_text().strip() if p.is_file() else None
    except (OSError, ValueError):
        return None


def load_config(env=None):
    """Return every setting as a dict, with defaults applied and the Gemini key resolved."""
    src = os.environ if env is None else env
    cfg = {}
    for name in STRINGS:
        value = str(src.get(name) or "").strip()
        cfg[name] = value or DEFAULTS.get(name, "")
    cfg["NOCODB_URL"] = cfg["NOCODB_URL"].rstrip("/")
    try:
        n = int(str(src.get("MAX_MESSAGES") or "").strip())
    except ValueError:
        n = 0
    cfg["MAX_MESSAGES"] = n if n > 0 else DEFAULT_MAX_MESSAGES

    path_var = str(src.get("GEMINI_API_KEY_PATH") or "").strip()
    if path_var:
        cfg["GEMINI_API_KEY"] = _read_key_file(path_var) or ""
    elif cfg["GEMINI_API_KEY"]:
        from_file = _read_key_file(cfg["GEMINI_API_KEY"])
        if from_file is not None:
            cfg["GEMINI_API_KEY"] = from_file
    return cfg


def _valid_nocodb_url(url):
    """https only; plain http only for this machine (tests, local use). Anything else could be
    redirected or read on the way, and the token travels in a header."""
    url = url.lower()  # the scheme and host are case-insensitive
    if url.startswith("https://"):
        return len(url) > len("https://")
    for prefix in ("http://localhost", "http://127.0.0.1"):
        if url.startswith(prefix) and (len(url) == len(prefix) or url[len(prefix)] in ":/"):
            return True
    return False


def missing_for_run(cfg):
    """Names (never values) of the settings a run needs that are empty or invalid."""
    out = [name for name in REQUIRED_FOR_RUN if not str(cfg.get(name) or "").strip()]
    url = str(cfg.get("NOCODB_URL") or "").strip()
    if url and not _valid_nocodb_url(url):
        out.append("NOCODB_URL")
    return out

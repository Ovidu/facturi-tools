"""Zoho Mail client: just what the daily invoice job needs.

Hosts come from the ZOHO_DC setting (`accounts.zoho.<dc>`, `mail.zoho.<dc>`). The access token
lives only on the instance. Errors carry the HTTP status and the service's own error text;
every secret this object holds is scrubbed from that text before it is raised.
"""
import json

from facturi_tools import httpx


class ZohoError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


DATA_CENTRES = ("eu", "com", "in", "com.au", "jp", "com.cn", "sa", "ca")


class ZohoMail:
    def __init__(self, cfg, http=httpx.request):
        self._cfg = cfg
        self._http = http
        dc = (cfg.get("ZOHO_DC") or "eu").strip()
        if dc not in DATA_CENTRES:
            # the refresh token goes to this host, so never build it from an arbitrary value
            raise ZohoError("ZOHO_DC is not a known Zoho data centre")
        self._accounts_host = f"https://accounts.zoho.{dc}"
        self._api = f"https://mail.zoho.{dc}/api"
        self._token = None
        self._account = None
        self._mailbox = None

    # ---- plumbing ----

    def _scrub(self, text):
        for s in (self._cfg.get("ZOHO_CLIENT_SECRET"), self._cfg.get("ZOHO_REFRESH_TOKEN"), self._token):
            if s:
                text = text.replace(s, "***")
        return text

    def _fail(self, status, text):
        label = f"HTTP {status}" if status is not None else "Zoho"
        return ZohoError(self._scrub(f"{label} {text}".strip()), status=status)

    @staticmethod
    def _parse(body):
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            return None
        return parsed

    @staticmethod
    def _envelope_ok(code):
        """The envelope code is success when absent or 2xx (Zoho answers 201 Created inside HTTP 200)."""
        if code is None:
            return True
        if isinstance(code, bool):
            return False
        try:
            return 200 <= int(code) < 300
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _error_text(parsed):
        """The service's own error words from a JSON answer, or '' when it is not an error."""
        if not isinstance(parsed, dict):
            return ""
        parts = []
        err = parsed.get("error")
        if err:
            parts.append(err if isinstance(err, str) else json.dumps(err))
        data = parsed.get("data")
        if isinstance(data, dict):
            for k in ("errorCode", "message"):
                if data.get(k):
                    parts.append(str(data[k]))
        status = parsed.get("status")
        if isinstance(status, dict) and not ZohoMail._envelope_ok(status.get("code")):
            parts.append(f"status {status.get('code')} {status.get('description', '')}".strip())
        return " ".join(parts)

    def _send(self, *args, **kw):
        try:
            return self._http(*args, **kw)
        except httpx.NetworkError as e:
            raise self._fail(None, f"network error: {e}")

    # ---- token ----

    def refresh(self):
        status, _, body = self._send("POST", f"{self._accounts_host}/oauth/v2/token", data={
            "grant_type": "refresh_token",
            "refresh_token": self._cfg.get("ZOHO_REFRESH_TOKEN", ""),
            "client_id": self._cfg.get("ZOHO_CLIENT_ID", ""),
            "client_secret": self._cfg.get("ZOHO_CLIENT_SECRET", ""),
        })
        parsed = self._parse(body)
        token = parsed.get("access_token") if isinstance(parsed, dict) else None
        if status != 200 or not token:
            text = self._error_text(parsed) or "no access_token in the answer"
            raise self._fail(status, f"token refresh failed: {text}")
        self._token = token

    def _call(self, method, path, params=None, json_body=None, raw=False):
        if self._token is None:
            self.refresh()
        for attempt in (0, 1):
            status, headers, body = self._send(
                method, self._api + path, headers={"Authorization": "Zoho-oauthtoken " + self._token},
                params=params, json_body=json_body)
            if status == 401 and attempt == 0:
                self.refresh()
                continue
            break
        parsed = self._parse(body) if (not raw or body.lstrip()[:1] == b"{") else None
        text = self._error_text(parsed)
        if not 200 <= status < 300:
            # a redirect is a failure too: httpx hands 3xx back instead of following it
            raise self._fail(status, text or "request failed")
        if text:
            raise self._fail(status, text)
        if raw:
            if not body:
                raise self._fail(status, "empty download")
            return body
        if not isinstance(parsed, dict):
            raise self._fail(status, "empty or unreadable answer")
        return parsed

    # ---- mailbox ----

    def account_id(self):
        if self._account is None:
            data = self._call("GET", "/accounts").get("data")
            if not isinstance(data, list) or not data or not data[0].get("accountId"):
                raise self._fail(None, "no account in the answer")
            self._account = str(data[0]["accountId"])
            self._mailbox = data[0].get("primaryEmailAddress") or data[0].get("emailAddress") or ""
        return self._account

    def folders(self):
        data = self._call("GET", f"/accounts/{self.account_id()}/folders").get("data") or []
        return {f["folderName"]: str(f["folderId"]) for f in data if f.get("folderName") and f.get("folderId")}

    def ensure_folder(self, name):
        found = self.folders().get(name)
        if found:
            return found
        out = self._call("POST", f"/accounts/{self.account_id()}/folders", json_body={"folderName": name})
        data = out.get("data")
        if isinstance(data, dict) and data.get("folderId"):
            return str(data["folderId"])
        found = self.folders().get(name)
        if not found:
            raise self._fail(None, "folder was not created")
        return found

    def list_folder(self, folder_id, limit):
        data = self._call("GET", f"/accounts/{self.account_id()}/messages/view",
                          params={"folderId": folder_id, "limit": limit}).get("data") or []

        def received(m):
            try:
                return int(m.get("receivedTime"))
            except (TypeError, ValueError):
                return 0
        return sorted(data, key=received)

    def attachments(self, folder_id, message_id):
        out = self._call("GET", f"/accounts/{self.account_id()}/folders/{folder_id}/messages/{message_id}/attachmentinfo")
        data = out.get("data")
        items = data.get("attachments") if isinstance(data, dict) else data
        res = []
        for a in items or []:
            if not isinstance(a, dict) or a.get("isInline") or a.get("inline") is True:
                continue
            size = a.get("attachmentSize")
            try:
                size = int(size)
            except (TypeError, ValueError):
                pass
            res.append({"attachmentId": str(a.get("attachmentId")), "attachmentName": a.get("attachmentName"),
                        "attachmentSize": size})
        return res

    def download(self, folder_id, message_id, attachment_id):
        return self._call(
            "GET", f"/accounts/{self.account_id()}/folders/{folder_id}/messages/{message_id}/attachments/{attachment_id}",
            raw=True)

    def move(self, message_ids, dest_folder_id):
        if not message_ids:
            return
        self._call("PUT", f"/accounts/{self.account_id()}/updatemessage",
                   json_body={"mode": "moveMessage", "messageId": list(message_ids), "destfolderId": dest_folder_id})

    def send(self, to, subject, html):
        aid = self.account_id()
        sender = (self._cfg.get("REPORT_FROM") or "").strip() or self._mailbox
        self._call("POST", f"/accounts/{aid}/messages", json_body={
            "fromAddress": sender, "toAddress": to, "subject": subject, "content": html, "mailFormat": "html"})

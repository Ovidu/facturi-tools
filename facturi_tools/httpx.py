"""A tiny HTTP helper on the standard library.

Every client in this package takes an optional `http=` callable with the signature of
`request`, so tests inject a fake and nothing here needs the network to be tested. Nothing in
this module logs or prints; transport errors name the host and the exception class only.
"""
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib would forward the caller's token headers to the new target."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class NetworkError(Exception):
    """A transport failure (timeout, refused connection, DNS). Never carries a URL query or header."""


def request(method, url, headers=None, params=None, json_body=None, data=None, timeout=60):
    """Send one request and return (status, headers, body). HTTP error statuses are returned."""
    if json_body is not None and data is not None:
        raise ValueError("pass json_body or data, not both")
    hdrs = dict(headers or {})
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif isinstance(data, dict):
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif data is not None:
        body = bytes(data)
    req = urllib.request.Request(url, data=body, method=method.upper(), headers=hdrs)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except OSError:
            payload = b""
        return e.code, dict(e.headers.items()) if e.headers else {}, payload
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        host = urllib.parse.urlsplit(url).hostname or "?"
        raise NetworkError(f"{host}: {type(e).__name__}") from None

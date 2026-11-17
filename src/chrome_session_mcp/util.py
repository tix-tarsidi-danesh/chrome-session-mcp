"""Shared plumbing: errors, HTTP, TLS, login-page detection, output sizing."""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

CACHE_DIR = os.path.expanduser(os.environ.get("CHROME_SESSION_MCP_CACHE", "~/.cache/chrome-session-mcp"))

# Tool results longer than this are written to a file instead of being returned
# inline: a huge result fills the agent's context window, and some clients reject
# it. The agent gets the file path plus a preview. Set from config (max_inline).
MAX_INLINE = 60000


class ToolError(Exception):
    """A failure the agent should read and act on (bad input, HTTP error, ...)."""


class AuthExpired(Exception):
    """The site rejected our cookies: the browser session has to be renewed."""

    def __init__(self, site, detail=""):
        super().__init__(f"{site}: session expired or missing ({detail})" if detail else site)
        self.site = site
        self.detail = detail


def log(*parts):
    # stdout carries the JSON-RPC stream, so every diagnostic goes to stderr.
    print("[chrome-session-mcp]", *parts, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- tls
def _ssl_context():
    """A verifying context that also works on python.org builds.

    Those ship an empty CA store until `Install Certificates.command` is run, which
    otherwise fails every request with CERTIFICATE_VERIFY_FAILED.
    """
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca"):
        return ctx
    try:
        import certifi  # type: ignore
        cands = [certifi.where()]
    except ImportError:
        cands = []
    for cand in cands + ["/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"]:
        if os.path.exists(cand):
            try:
                ctx.load_verify_locations(cand)
                return ctx
            except OSError:
                continue
    return ctx


SSL_CTX = _ssl_context()


# -------------------------------------------------------------------------- http
class Response:
    __slots__ = ("status", "url", "body", "headers")

    def __init__(self, status, url, body, headers):
        self.status = status
        self.url = url
        self.body = body
        self.headers = headers

    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            raise ToolError(f"expected JSON from {self.url}, got HTTP {self.status}: {self.text()[:300]}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Stop at redirects for non-GET requests, so a login redirect stays visible."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.get_method() != "GET":
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=SSL_CTX))


def http(method, url, headers=None, data=None, timeout=90):
    """One HTTP call. Never raises on an HTTP status; network errors become ToolError.

    A GET that fails at the network level (connect timeout, reset) is retried
    once: proxies and VPN edges drop the odd connection, and a GET is safe to repeat.
    """
    attempts = 2 if method == "GET" else 1
    for attempt in range(attempts):
        req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return Response(r.status, r.geturl(), r.read(), r.headers)
        except urllib.error.HTTPError as e:
            return Response(e.code, url, e.read(), e.headers)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt + 1 < attempts:
                log(f"{method} {url}: {e}; retrying once")
                time.sleep(1)
                continue
            reason = getattr(e, "reason", None) or f"{type(e).__name__}: {e}"
            raise ToolError(f"{method} {url}: {reason}. Check the VPN / network, then retry.")


# Text that Google IAP and common SSO front doors put on their login pages.
LOGIN_MARKERS = ("accounts.google.com", "Sign in with Google", "continue to IAP", "GCP_IAP",
                 "Auth Cookie is invalid", "Cookie Expired")


def looks_like_login(resp):
    """True when an SSO front door (e.g. Google IAP) answered instead of the app.

    IAP answers an expired cookie with a redirect: followed for GET (we land on
    accounts.google.com), but returned as-is for POST, e.g.
    `302 Auth Cookie is invalid: Cookie Expired`.
    """
    if "accounts.google.com" in (resp.url or ""):
        return True
    if 300 <= resp.status < 400:
        loc = (resp.headers.get("Location") or "") if resp.headers else ""
        if not loc or "accounts.google.com" in loc or "/_gcp_iap/" in loc or "login" in loc.lower():
            return True
    head = resp.body[:4000].decode("utf-8", "replace")
    return any(m in head for m in LOGIN_MARKERS)


# ------------------------------------------------------------------------ output
def cache_path(*parts):
    p = os.path.join(CACHE_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def write_private(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def fit(text, label="result"):
    """Return text as-is, or spill it to a file and return a pointer + preview."""
    if len(text) <= MAX_INLINE:
        return text
    path = cache_path("out", f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    write_private(path, text)
    preview = text[: min(4000, MAX_INLINE)]
    return (f"[{len(text):,} chars — too large to return inline; full output written to {path}. "
            f"Read that file (or narrow the query). Preview of the first {len(preview):,} chars:]\n\n"
            + preview)


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


def resolve_path(p):
    """Expand ~ and make relative paths absolute (relative to the server's cwd)."""
    if not p:
        return p
    return os.path.abspath(os.path.expanduser(p))


def host_of(url):
    return url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1]

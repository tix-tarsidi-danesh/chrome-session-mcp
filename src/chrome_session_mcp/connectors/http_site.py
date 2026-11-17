"""Any web app or API that accepts your browser session cookies.

Config:  {"type": "http", "base_url": "https://admin.example.com",
          "probe": "/api/me",                      # a GET that succeeds only when logged in
          "headers": {"X-Requested-With": "XMLHttpRequest"},   # optional static headers
          "csrf": {"cookie": "XSRF-TOKEN", "header": "X-XSRF-TOKEN"},  # optional
          "allow_write": false}

Use it for internal tools that have no dedicated connector: an admin API, a
feature-flag console, a data explorer. Requests only ever go to the
connector's own host, so its cookies cannot leak to another site. Without
allow_write only GET / HEAD are allowed.
"""
import json
import urllib.parse

from ..registry import Connector, I, S, connector_type, tool
from ..util import ToolError, dumps, fit


@connector_type("http")
class HttpSite(Connector):
    options = ("csrf",)

    def request(self, method, path, headers=None, data=None, json_body=None, timeout=90):
        h = dict(headers or {})
        csrf = self.cfg.get("csrf")
        if csrf and method not in ("GET", "HEAD"):
            jar = dict(p.split("=", 1) for p in self.cookie().split("; ") if "=" in p)
            token = jar.get(csrf.get("cookie", ""))
            if token:
                h[csrf.get("header", "X-CSRF-Token")] = urllib.parse.unquote(token)
        return super().request(method, path, h, data, json_body, timeout)


def _render(r, max_chars):
    ctype = (r.headers.get("Content-Type") or "") if r.headers else ""
    out = {"status": r.status, "url": r.url, "content_type": ctype}
    try:
        out["json"] = json.loads(r.body)
    except ValueError:
        text = r.text()
        out["text"] = text[:max_chars] if max_chars else text
        if max_chars and len(text) > max_chars:
            out["truncated"] = f"{len(text):,} chars, showing {max_chars:,}"
    return fit(dumps(out), "http")


@tool("http_get", "GET from a site",
      "GET a path on an `http` connector's site, authenticated with the user's Chrome session. "
      "Returns status, content type, and the JSON body (or text).",
      {"path": S("Path on the site, e.g. /api/orders/123 (a full URL on the same host also works)."),
       "params": {"type": "object", "additionalProperties": {"type": "string"},
                  "description": "Query parameters."},
       "max_chars": I("Truncate a non-JSON body to this many chars (default 20000, 0 = all).", default=20000)},
      required=["path"], type="http")
def http_get(c, path, params=None, max_chars=20000):
    if params:
        path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params)
    return _render(c.call("GET", path, ok=(200, 499)), max_chars)


@tool("http_request", "Send a request to a site",
      "POST / PUT / PATCH / DELETE to an `http` connector's site with a JSON or form body. Only available "
      "on connectors with allow_write.",
      {"method": S("HTTP method.", enum=["POST", "PUT", "PATCH", "DELETE"]),
       "path": S("Path on the site."),
       "json": {"type": "object", "description": "JSON body."},
       "form": {"type": "object", "additionalProperties": {"type": "string"},
                "description": "Form body (application/x-www-form-urlencoded), instead of json."},
       "max_chars": I("Truncate a non-JSON response body (default 20000, 0 = all).", default=20000)},
      required=["method", "path"], type="http", write=True, destructive=True)
def http_request(c, method, path, json=None, form=None, max_chars=20000):
    if json is not None and form is not None:
        raise ToolError("pass json or form, not both.")
    if form is not None:
        r = c.call(method, path, headers={"Content-Type": "application/x-www-form-urlencoded"},
                   data=urllib.parse.urlencode(form).encode(), ok=(200, 499))
    else:
        r = c.call(method, path, json_body=json, ok=(200, 499))
    return _render(r, max_chars)

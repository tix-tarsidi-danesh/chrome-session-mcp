"""Connector types, tool definitions, and the base class every connector extends.

A *connector type* (e.g. "grafana") is a Connector subclass registered with
@connector_type. A *connector instance* is one entry in the config file
("grafana-prod": {"type": "grafana", "base_url": ...}).

Tools are declared once per type with @tool(..., type="grafana"). Each tool
gets an `instance` argument listing the configured instances of its type, so
five Grafanas still mean one set of Grafana tools. A tool only appears in
tools/list when its type is configured; a write tool only appears when some
instance of its type has "allow_write": true.

Custom connectors (in a private module listed under "plugins" in the config)
use exactly the same two decorators.
"""
import json

from . import chrome
from .auth import Site, with_login
from .util import AuthExpired, ToolError, host_of, http, looks_like_login

TYPES = {}   # type name -> Connector subclass
TOOLS = {}   # tool name -> ToolDef


class ToolDef:
    __slots__ = ("name", "title", "description", "props", "required", "type", "write",
                 "destructive", "handler")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def tool(name, title, description, props=None, required=(), *, type=None, write=False,
         destructive=False):
    """Register a tool. With `type`, the handler's first argument is the resolved instance."""
    def deco(fn):
        if name in TOOLS:
            raise ValueError(f"tool {name!r} is registered twice")
        TOOLS[name] = ToolDef(name=name, title=title, description=description, props=props or {},
                              required=list(required), type=type, write=write,
                              destructive=destructive, handler=fn)
        return fn
    return deco


def connector_type(name):
    """Register a Connector subclass under a config type name."""
    def deco(cls):
        if name in TYPES:
            raise ValueError(f"connector type {name!r} is registered twice")
        cls.type_name = name
        TYPES[name] = cls
        return cls
    return deco


# ------------------------------------------------------------------- schemas
def S(desc, **kw):
    return {"type": "string", "description": desc, **kw}


def I(desc, **kw):
    return {"type": "integer", "description": desc, **kw}


def B(desc, default=False):
    return {"type": "boolean", "description": desc, "default": default}


# ----------------------------------------------------------------- connector
class Connector(Site):
    """One configured site, authenticated with the user's Chrome cookies.

    Subclasses set `probe_path` (a cheap GET that succeeds only when logged in)
    and may override `is_expired` to recognize their own "please log in"
    answer. The base class handles cookies, IAP detection, and recovery.
    """

    type_name = ""
    probe_path = "/"
    login_path = "/"
    requires_base_url = True
    # Extra config keys this type accepts (documented in the README).
    options = ()

    COMMON = ("type", "base_url", "login_url", "probe", "allow_write", "cookie_host", "headers",
              "description")

    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        base = (cfg.get("base_url") or "").rstrip("/")
        if self.requires_base_url and not base.startswith(("http://", "https://")):
            raise ValueError(f"{name}: base_url must be an http(s) URL, got {base!r}")
        self.base_url = base
        self.host = cfg.get("cookie_host") or (host_of(base) if base else "")
        self.login_url = cfg.get("login_url") or (base + self.login_path if base else "")
        self.allow_write = bool(cfg.get("allow_write"))
        self.extra_headers = dict(cfg.get("headers") or {})
        self._cookie = None

    def describe(self):
        return {"type": self.type_name, "base_url": self.base_url or None,
                "allow_write": self.allow_write, "description": self.cfg.get("description")}

    def matches_url(self, url):
        """True when `url` belongs to this connector (used to pick an instance by URL)."""
        h = host_of(url)
        return h == self.host or (bool(self.base_url) and h == host_of(self.base_url))

    # -- auth -------------------------------------------------------------
    def reload(self):
        self._cookie = chrome.cookie_header(self.host)

    def cookie(self):
        if self._cookie is None:
            self.reload()
        if not self._cookie:
            raise AuthExpired(self.name, f"no Chrome cookies for {self.host}")
        return self._cookie

    def is_expired(self, resp):
        return looks_like_login(resp) or resp.status == 401

    def probe(self):
        try:
            r = self.request("GET", self.cfg.get("probe") or self.probe_path)
        except AuthExpired:
            return False
        except ToolError:
            return False
        return r.status < 400

    # -- http -------------------------------------------------------------
    def url(self, path):
        if path.startswith(("http://", "https://")):
            if host_of(path) != host_of(self.base_url or path):
                raise ToolError(f"{self.name}: refusing to send its cookies to another host: {path}")
            return path
        return self.base_url + ("" if path.startswith("/") else "/") + path

    def request(self, method, path, headers=None, data=None, json_body=None, timeout=90):
        """One authenticated request. Raises AuthExpired; never retries a login."""
        h = {"Cookie": self.cookie(), "Accept": "application/json, text/plain, */*"}
        h.update(self.extra_headers)
        if json_body is not None:
            data = json.dumps(json_body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        r = http(method, self.url(path), h, data, timeout)
        if self.is_expired(r):
            raise AuthExpired(self.name, f"HTTP {r.status}")
        return r

    def call(self, method, path, headers=None, data=None, json_body=None, timeout=90, ok=(200, 299)):
        """A request with login recovery. HTTP errors become ToolError."""
        def go():
            r = self.request(method, path, headers, data, json_body, timeout)
            if not ok[0] <= r.status <= ok[1]:
                raise ToolError(f"{self.name}: {method} {path} -> HTTP {r.status}: {r.text()[:600]}")
            return r
        return with_login(self, go)

    def get_json(self, path, **kw):
        return self.call("GET", path, **kw).json()

    def require_write(self):
        if not self.allow_write:
            raise ToolError(f"{self.name}: writes are disabled. Set \"allow_write\": true on this "
                            "connector in the config to enable them.")

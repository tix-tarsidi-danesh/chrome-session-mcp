"""Tools that exist whatever is configured."""
import urllib.parse

from . import auth, chrome
from .config import RUNTIME
from .registry import TYPES, I, S, tool
from .util import ToolError, dumps

EXAMPLE = {
    "connectors": {
        "grafana": {"type": "grafana", "base_url": "https://grafana.example.com"},
        "slack": {"type": "slack", "workspace": "example.slack.com"},
    }
}


@tool("list_connectors", "List configured connectors",
      "List the connectors in the config file (name, type, base URL, whether writes are enabled), "
      "plus any config errors. Call this first to learn which instance names to pass to other tools.")
def list_connectors():
    out = {"config_file": RUNTIME.path,
           "connectors": {n: c.describe() for n, c in RUNTIME.instances.items()}}
    if RUNTIME.errors:
        out["errors"] = RUNTIME.errors
    if not RUNTIME.loaded:
        out["setup"] = (f"No config file yet. Create {RUNTIME.path} like the example below, then "
                        "restart the MCP server. Available types: " + ", ".join(sorted(TYPES)))
        out["example"] = EXAMPLE
    return dumps(out)


@tool("auth_status", "Check logins",
      "Check which connectors currently have a working Chrome session, without opening a browser.",
      {"instances": {"type": "array", "items": {"type": "string"},
                     "description": "Connector names to check (default: all)."}})
def auth_status(instances=None):
    out = {}
    for name in instances or list(RUNTIME.instances):
        c = RUNTIME.instances.get(name)
        if c is None:
            out[name] = "not configured"
            continue
        try:
            c.reload()
            ok = c.probe()
            prof = chrome.profile_for(c.host) if c.host else None
            out[name] = ("ok" if ok else f"not logged in — log in to {c.login_url} (or call login)") \
                + (f" [Chrome profile '{prof}']" if prof else "")
        except ToolError as e:
            out[name] = f"error: {e}"
    for name, err in RUNTIME.errors.items():
        out.setdefault(name, f"config error: {err}")
    return dumps(out)


@tool("login", "Log in through Chrome",
      "Open Chrome on a connector's login page and wait until its session works. Other tools already "
      "do this by themselves when they hit an expired session; use this to log in ahead of time.",
      {"instance": S("Connector name (see list_connectors)."),
       "wait_seconds": I("How long to wait for the login (default from settings, 180).")},
      required=["instance"])
def login(instance, wait_seconds=None):
    c = RUNTIME.instances.get(instance)
    if c is None:
        raise ToolError(f"no connector named {instance!r}. Configured: {', '.join(RUNTIME.instances) or 'none'}.")
    return auth.login(c, wait=wait_seconds, force_open=True)


@tool("open_in_browser", "Open a page in Chrome",
      "Open a URL in the user's own Google Chrome so they can look at it themselves (a dashboard, a "
      "failing build, a page you just edited). It opens in the Chrome profile that holds that site's "
      "cookies, so the page is already logged in. Only http/https URLs.",
      {"url": S("The page to open (http or https)."),
       "profile": S("Chrome profile directory to use instead, e.g. 'Default' or 'Profile 1'.")},
      required=["url"], write=True)
def open_in_browser(url, profile=None):
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ToolError(f"refused: only http/https URLs can be opened, got {url!r}.")
    prof = chrome.open_url(parsed.geturl(), parsed.hostname, profile)
    return f"Opened {parsed.geturl()} in Chrome (profile '{prof}')."


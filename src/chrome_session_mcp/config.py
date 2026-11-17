"""Load the config file and build the connector instances.

Location: $CHROME_SESSION_MCP_CONFIG, else ~/.config/chrome-session-mcp/config.json.

{
  "settings":     {"login_wait": 180, "open_browser": true, "max_inline": 60000,
                   "chrome_profile": null, "chrome_user_data": null},
  "plugin_paths": ["~/acme-connectors"],          # added to sys.path
  "plugins":      ["acme_connectors.data_explorer"],  # imported; register their own types
  "connectors": {
    "grafana-prod": {"type": "grafana", "base_url": "https://grafana.example.com"},
    ...
  }
}

A broken connector entry never stops the server: it is reported by
list_connectors / auth_status, and the other connectors keep working.
"""
import importlib
import json
import os
import sys

from . import auth, chrome, util
from .registry import TYPES, Connector
from .util import ToolError, log

DEFAULT_PATH = "~/.config/chrome-session-mcp/config.json"


def config_path():
    return os.path.expanduser(os.environ.get("CHROME_SESSION_MCP_CONFIG") or DEFAULT_PATH)


class Runtime:
    def __init__(self):
        self.path = config_path()
        self.instances = {}   # name -> Connector
        self.errors = {}      # name (or "<config>") -> message
        self.loaded = False

    # -- loading ------------------------------------------------------------
    def load(self, data=None):
        """Build instances from `data` (a dict) or from the config file."""
        self.instances, self.errors = {}, {}
        if data is None:
            if not os.path.exists(self.path):
                self.loaded = False
                return self
            try:
                with open(self.path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as e:
                self.errors["<config>"] = f"cannot read {self.path}: {e}"
                return self
        self.loaded = True
        if not isinstance(data, dict):
            self.errors["<config>"] = "the config must be a JSON object"
            return self

        s = data.get("settings") or {}
        auth.configure(s.get("login_wait"), s.get("open_browser"))
        chrome.configure(s.get("chrome_profile"), s.get("chrome_user_data"))
        if s.get("max_inline"):
            util.MAX_INLINE = int(s["max_inline"])

        for p in data.get("plugin_paths") or []:
            p = os.path.abspath(os.path.expanduser(p))
            if p not in sys.path:
                sys.path.insert(0, p)
        for mod in data.get("plugins") or []:
            try:
                importlib.import_module(mod)
            except Exception as e:  # a broken plugin must not take the server down
                self.errors[f"<plugin {mod}>"] = f"{type(e).__name__}: {e}"
                log(f"plugin {mod} failed to load: {e}")

        connectors = data.get("connectors")
        connectors = {} if connectors is None else connectors
        if not isinstance(connectors, dict):
            self.errors["<config>"] = "\"connectors\" must be an object of name -> settings"
            return self
        for name, cfg in connectors.items():
            try:
                self.instances[name] = self._build(name, cfg)
            except Exception as e:
                self.errors[name] = str(e)
                log(f"connector {name}: {e}")
        return self

    def _build(self, name, cfg):
        if not isinstance(cfg, dict):
            raise ValueError(f"{name}: settings must be an object")
        t = cfg.get("type")
        cls = TYPES.get(t)
        if cls is None:
            raise ValueError(f"{name}: unknown type {t!r}. Known: {', '.join(sorted(TYPES))} "
                             "(custom types come from \"plugins\").")
        unknown = set(cfg) - set(Connector.COMMON) - set(cls.options)
        if unknown:
            raise ValueError(f"{name}: unknown setting(s) {', '.join(sorted(unknown))} for type "
                             f"{t}. Allowed: {', '.join(sorted(set(Connector.COMMON) | set(cls.options)))}")
        return cls(name, cfg)

    # -- lookups ------------------------------------------------------------
    def of_type(self, type_name, write=False):
        return [c for c in self.instances.values()
                if c.type_name == type_name and (c.allow_write or not write)]

    def resolve(self, type_name, instance=None, write=False, url=None):
        cands = self.of_type(type_name, write)
        if instance:
            for c in cands:
                if c.name == instance:
                    return c
            other = self.instances.get(instance)
            if other is not None and other.type_name == type_name and write:
                other.require_write()
            raise ToolError(f"no {type_name} connector named {instance!r}. "
                            f"Configured: {', '.join(c.name for c in cands) or 'none'}.")
        if len(cands) == 1:
            return cands[0]
        if url and url.startswith(("http://", "https://")):
            for c in cands:
                if c.matches_url(url):
                    return c
        if not cands:
            raise ToolError(f"no {type_name} connector is configured"
                            + (" with allow_write" if write else "") + ".")
        raise ToolError(f"several {type_name} connectors are configured; pass instance "
                        f"(one of: {', '.join(c.name for c in cands)}).")


RUNTIME = Runtime()

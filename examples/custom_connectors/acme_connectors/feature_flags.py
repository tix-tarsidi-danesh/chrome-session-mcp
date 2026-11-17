"""Example custom connector: an internal feature-flag console.

Copy this folder somewhere private, then point the config at it:

    {
      "plugin_paths": ["~/acme-connectors"],
      "plugins": ["acme_connectors.feature_flags"],
      "connectors": {
        "flags": {"type": "acme-flags", "base_url": "https://flags.acme.internal",
                  "environment": "production"}
      }
    }

A connector type is a Connector subclass registered with @connector_type.
Its tools are plain functions registered with @tool(type=...): the server
adds the `instance` argument, resolves it, and passes the connector as the
first argument. Raise ToolError for a message the agent should see.
"""
from chrome_session_mcp.registry import Connector, B, S, connector_type, tool
from chrome_session_mcp.util import ToolError, dumps, fit


@connector_type("acme-flags")
class FeatureFlags(Connector):
    # A cheap GET that succeeds only when the session is valid.
    probe_path = "/api/me"
    # Where to send the user when the session has expired.
    login_path = "/login"
    # Extra config keys this type accepts (on top of the common ones).
    options = ("environment",)

    def is_expired(self, resp):
        # Add app-specific "please log in" answers to the default IAP / 401 check.
        return super().is_expired(resp) or resp.status == 403

    @property
    def environment(self):
        return self.cfg.get("environment", "production")


@tool("flags_get", "Read a feature flag",
      "Read one feature flag and its rules in the configured environment.",
      {"key": S("Flag key, e.g. new-checkout.")},
      required=["key"], type="acme-flags")
def flags_get(f, key):
    if not key.replace("-", "").replace("_", "").isalnum():
        raise ToolError(f"invalid flag key: {key!r}")
    data = f.get_json(f"/api/flags/{key}", headers={"X-Environment": f.environment})
    return fit(dumps(data), "flags")


@tool("flags_set", "Turn a feature flag on or off",
      "Turn a feature flag on or off. Needs \"allow_write\": true on the connector.",
      {"key": S("Flag key."), "enabled": B("New state.")},
      required=["key", "enabled"], type="acme-flags", write=True, destructive=True)
def flags_set(f, key, enabled):
    r = f.call("PATCH", f"/api/flags/{key}", json_body={"enabled": bool(enabled)},
               headers={"X-Environment": f.environment})
    return dumps({"key": key, "enabled": bool(enabled), "status": r.status})

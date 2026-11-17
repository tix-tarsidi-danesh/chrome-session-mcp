# chrome-session-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an AI agent (Claude Code,
Claude Desktop, Cursor, or any MCP client) use the internal web apps **you are already
logged in to in Google Chrome**: Grafana, Slack, Jenkins, SonarQube, Confluence,
Kibana, or any site that authenticates with a browser cookie.

There are no API tokens, OAuth apps, or service accounts to set up. The server reads
your own Chrome session cookies, so the agent sees exactly what you see and nothing
more. When a session expires, it opens Chrome on the login page, waits for you to log
in, and then retries the call.

```
agent ──MCP──▶ chrome-session-mcp ──HTTPS + your cookies──▶ grafana.acme.internal
                     │                                      slack.com
                     └─ reads ~/…/Chrome/<profile>/Cookies    jenkins.acme.internal …
```

- **Standard library only.** Python 3.9+ and the system `openssl`. Nothing to `pip install` at run time.
- **Config-driven.** Name your instances (`grafana-prod`, `grafana-staging`, …). Tools appear only for the connector types you configure.
- **Read-only by default.** Write tools (post to Slack, edit Confluence, POST to an API) are hidden until you set `"allow_write": true` on that connector.
- **Extensible.** Add a private connector for your own internal tool in about 40 lines (see [Custom connectors](#custom-connectors)).

> **Platforms:** macOS and Linux. Windows is not supported: Chrome on Windows encrypts
> cookies with app-bound encryption, which another process cannot decrypt.

## Contents

- [Quick start](#quick-start)
- [How login recovery works](#how-login-recovery-works)
- [Configuration](#configuration)
- [Connectors and tools](#connectors-and-tools)
- [Custom connectors](#custom-connectors)
- [Security model](#security-model)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Quick start

**1. Install.** Any of these work:

```bash
pipx install chrome-session-mcp            # or: uv tool install chrome-session-mcp
# or run it straight from a checkout, with nothing installed:
git clone https://github.com/tix-tarsidi-danesh/chrome-session-mcp && export PYTHONPATH=$PWD/chrome-session-mcp/src
```

**2. Write a config** at `~/.config/chrome-session-mcp/config.json`
(or point `CHROME_SESSION_MCP_CONFIG` at another path):

```json
{
  "connectors": {
    "grafana": {"type": "grafana", "base_url": "https://grafana.acme.internal"},
    "slack":   {"type": "slack", "workspace": "acme.slack.com"},
    "jenkins": {"type": "jenkins", "base_url": "https://jenkins.acme.internal",
                "job_path": "job/team/job/checkout-service"}
  }
}
```

[`examples/config.example.json`](examples/config.example.json) shows every built-in type.

**3. Register the server with your MCP client.**

Claude Code:

```bash
claude mcp add chrome-session -- chrome-session-mcp
# from a checkout instead:
claude mcp add chrome-session -e PYTHONPATH=/path/to/chrome-session-mcp/src -- python3 -m chrome_session_mcp
```

Claude Desktop, Cursor, and other clients that use a JSON file:

```json
{
  "mcpServers": {
    "chrome-session": {
      "command": "chrome-session-mcp",
      "env": {"CHROME_SESSION_MCP_CONFIG": "/Users/me/.config/chrome-session-mcp/config.json"}
    }
  }
}
```

**4. Try it.** Ask the agent to "call `auth_status`". The first call that reads cookies
on macOS shows a Keychain prompt for **Chrome Safe Storage**. Choose **Always Allow**
so it is not asked again. The server reads that key once per process and keeps it in memory.

## How login recovery works

Every connector has a cheap **probe** request that succeeds only with a valid session
(for example Grafana `GET /api/user`). When a tool call is rejected as logged out, the
server does this:

1. It opens Chrome on the connector's `login_url`, in the profile the cookies came from.
   It opens the page at most once every 2 minutes per connector, so a retrying agent
   cannot fill your screen with tabs.
2. Every 3 seconds it re-reads Chrome's cookie database and runs the probe. Chrome
   writes new cookies to disk with a delay of up to about 30 seconds after the login,
   so the server polls instead of waiting a fixed time.
3. When the probe passes, it retries the original call **once**. If the wait runs out
   (default 180 s), the tool returns an error that tells the agent to retry after you log in.

"Logged out" is recognized in these forms:

| Answer | Typical source |
|--------|----------------|
| The response ends on `accounts.google.com`, or a 3xx redirect to a login page | Google IAP, most SSO front doors |
| `302` with `Auth Cookie is invalid` / `Cookie Expired` in the body | Google IAP answering a `POST` |
| `401` | Most APIs |
| `403` plus a redirect to `/securityRealm/commenceLogin` | Jenkins with an SSO realm |
| `api/authentication/validate` returns `valid: false` | SonarQube |
| The current user is `anonymous` | Confluence |
| `invalid_auth`, `not_authed`, `token_expired`, … | Slack |

Grafana is a special case. On a `401` the server first asks Grafana to rotate its own
session token (`POST /api/user/auth-tokens/rotate`) and only opens Chrome if that fails.

Set `CHROME_SESSION_MCP_NO_BROWSER=1` to never open Chrome (for CI, or a headless machine).
The tools then report the expiry and stop.

## Configuration

```jsonc
{
  "settings": {
    "login_wait": 180,          // seconds to wait for a login; 0 = never wait
    "open_browser": true,       // false = report expiry, never open Chrome
    "max_inline": 60000,        // larger tool results are written to a file instead
    "chrome_profile": null,     // e.g. "Profile 1"; default = the profile with the most cookies for the host
    "chrome_user_data": null    // Chrome's user-data dir, if not the default
  },
  "plugin_paths": ["~/acme-connectors"],          // added to sys.path
  "plugins": ["acme_connectors.feature_flags"],   // imported at start; they register their own types
  "connectors": {
    "<instance name>": {"type": "<type>", "base_url": "https://…", "…": "…"}
  }
}
```

**Settings every connector accepts:**

| Key | Meaning |
|-----|---------|
| `type` | Connector type (see below). Required. |
| `base_url` | Site root, e.g. `https://grafana.acme.internal`. Required for all types except `slack`. |
| `login_url` | Page to open when the session expires. Default: a type-specific page on `base_url`. |
| `probe` | Path of a `GET` that succeeds only when logged in. Overrides the type's default. |
| `cookie_host` | Read cookies for this host instead of `base_url`'s host (e.g. a parent domain). |
| `headers` | Extra static headers sent with every request. |
| `allow_write` | `true` makes this instance's write tools available. Default `false`. |
| `description` | Free text, shown by `list_connectors` to help the agent pick an instance. |

A broken entry (unknown type, unknown key, bad URL) does not stop the server. The other
connectors keep working, and `list_connectors` reports the error.

**Environment variables** (these override the config file):

| Variable | Meaning |
|----------|---------|
| `CHROME_SESSION_MCP_CONFIG` | Config file path. |
| `CHROME_SESSION_MCP_CACHE` | Cache directory (default `~/.cache/chrome-session-mcp`). |
| `CHROME_SESSION_MCP_LOGIN_WAIT` | Seconds to wait for a login. |
| `CHROME_SESSION_MCP_NO_BROWSER=1` | Never open Chrome. |
| `CHROME_PROFILE`, `CHROME_USER_DATA` | Pin the Chrome profile / user-data directory. |
| `CHROME_SAFE_STORAGE_PASSWORD` | Linux: the keyring password, if `secret-tool` cannot find it. |

## Connectors and tools

Each tool of a typed connector takes an `instance` argument. It is optional when only one
instance of that type is configured, or when the tool takes a `url` argument (the server
then picks the instance by the URL's host). Tools marked ✎ are write tools and appear only
on instances with `"allow_write": true`.

**Always available**

| Tool | What it does |
|------|--------------|
| `list_connectors` | The configured instances and any config errors. Call it first. Without a config file it returns setup help. |
| `auth_status` | Which instances have a working session right now. Never opens a browser. |
| `login` | Open Chrome on an instance's login page and wait until the session works. |
| `open_in_browser` | Open a URL in your Chrome (in the profile that is logged in to it), e.g. to show you a failing build. |

**`grafana`**: `{"type": "grafana", "base_url": "…"}`

| Tool | What it does |
|------|--------------|
| `grafana_resolve` | Follow a `/goto/` short link: dashboard URL, uid, variables, time range. |
| `grafana_dashboard` | A dashboard's variables and panels. The full JSON is written to a file. |
| `grafana_run_panel` | Run a panel's queries and return last / min / max / avg per series. |
| `grafana_get` | `GET` any Grafana HTTP API path. |

**`slack`**: `{"type": "slack", "workspace": "acme.slack.com"}` (or `"team_id": "T0123ABCD"`)

Slack's web client needs the `d` cookie and an `xoxc-` session token, which Slack keeps
in Chrome's localStorage. The connector reads both. If you are signed in to several
workspaces, it keeps the token whose `auth.test` matches `workspace` / `team_id`.

| Tool | What it does |
|------|--------------|
| `slack_channels` | List channels (cached for 24 h), filtered by a regex. |
| `slack_history` | A channel's messages in a time window. |
| `slack_thread` | A thread, from a message link or `channel` + `ts`. |
| `slack_search` | Slack search syntax, e.g. `in:#incidents timeout after:2026-09-01`. |
| `slack_user` | A user's profile by ID or email. |
| `slack_post` ✎ | Post a message or a thread reply as you. |

**`jenkins`** and **`sonarqube`**

```json
"ci":    {"type": "jenkins", "base_url": "https://jenkins.acme.internal",
          "job_path": "job/team/job/checkout-service", "sonar": "sonar", "sonar_project": "checkout-service"},
"sonar": {"type": "sonarqube", "base_url": "https://sonar.acme.internal"}
```

`job_path` is the default multibranch job, and the tools also accept a Blue Ocean or
classic Jenkins URL. `sonar` names a `sonarqube` instance. When it is set,
`jenkins_pr_status` includes that PR's quality gate.

| Tool | What it does |
|------|--------------|
| `jenkins_pr_status` | Every pipeline stage with status and duration, the log tail of each failed step, and the Sonar quality gate. |
| `jenkins_console_log` | A build's console output: the last N lines, or `grep` matches with context. |
| `sonar_quality_gate` | Quality-gate status and conditions for a PR, a branch, or the main branch. |

**`confluence`**: `{"type": "confluence", "base_url": "https://acme.atlassian.net"}`

For Server / Data Center, add `"api_path": "/rest/api"`. Page bodies are moved as
**storage-format XHTML through local files**, never as Markdown and never inline. A
Markdown round-trip breaks embedded images, and a long page's body can pass 250,000
characters, which is too large for a tool argument.

| Tool | What it does |
|------|--------------|
| `confluence_get_page` | Write a page's body to a file. Return title, version, size, and counts of images / macros / comment anchors. |
| `confluence_list_attachments` | A page's attachments. |
| `confluence_update_page` ✎ | Replace the body from a file. Refuses the update when the page changed since you read it (`expected_version`), when the body shrinks by more than half, or when images / macros / comment anchors would be lost. Backs up the old body first. |
| `confluence_create_page` ✎ | Create a page. |
| `confluence_upload_attachment` ✎ | Upload a file, e.g. a rendered diagram, to embed it. |
| `confluence_set_appearance` ✎ | Full-width or fixed-width layout. |

**`kibana`**: `{"type": "kibana", "base_url": "…"}`

Searches use Kibana's own search API (`/internal/search/ese`), so they work for anyone
who can open Discover. Tested against Kibana 7.17.

| Tool | What it does |
|------|--------------|
| `kibana_resolve` | Follow a `/goto/` short link: index pattern, query, time range. |
| `kibana_indices` | List index patterns. |
| `kibana_search` | Search a time window. Returns timestamp + message per hit, or whole documents. |
| `kibana_histogram` | Hits per time bucket, to see when an error started. |

**`http`**: any cookie-authenticated site or API

```json
"admin": {"type": "http", "base_url": "https://admin.acme.internal", "probe": "/api/me",
          "csrf": {"cookie": "XSRF-TOKEN", "header": "X-XSRF-TOKEN"}}
```

| Tool | What it does |
|------|--------------|
| `http_get` | `GET` a path on the site. Returns status, content type, and the JSON or text body. |
| `http_request` ✎ | `POST` / `PUT` / `PATCH` / `DELETE` with a JSON or form body. Sends the CSRF header from the named cookie. |

## Custom connectors

For an internal tool that needs more than `http_get`, write a connector in your own private
module and list it under `plugins`. A complete example is in
[`examples/custom_connectors/acme_connectors/feature_flags.py`](examples/custom_connectors/acme_connectors/feature_flags.py):

```python
from chrome_session_mcp.registry import Connector, S, connector_type, tool
from chrome_session_mcp.util import dumps

@connector_type("acme-flags")
class FeatureFlags(Connector):
    probe_path = "/api/me"         # a GET that succeeds only when logged in
    login_path = "/login"          # where to send the user on expiry
    options = ("environment",)     # extra config keys this type accepts

@tool("flags_get", "Read a feature flag", "Read one feature flag.",
      {"key": S("Flag key.")}, required=["key"], type="acme-flags")
def flags_get(f, key):
    return dumps(f.get_json(f"/api/flags/{key}"))
```

```json
{
  "plugin_paths": ["~/acme-connectors"],
  "plugins": ["acme_connectors.feature_flags"],
  "connectors": {"flags": {"type": "acme-flags", "base_url": "https://flags.acme.internal"}}
}
```

What the base class does for you:

- `f.call()` and `f.get_json()` send the cookies, recognize a logged-out answer,
  run login recovery, and turn HTTP errors into a `ToolError` message for the agent.
- `f.url()` refuses any absolute URL on another host, so the connector's cookies
  cannot be sent to a different site.
- Tools registered with `write=True` are hidden unless the instance has `allow_write`.
- Override `is_expired(resp)` to recognize your app's own "please log in" answer, and
  `request()` to add headers (for example a CSRF token or an SSO header read from a cookie).

Keep connectors that describe internal systems (hostnames, API paths, IDs) in a private
repository. Only the generic server needs to be public.

## Security model

Read [SECURITY.md](SECURITY.md) before you use this with production systems. In short:

- The server acts **as you**, with your browser session. Give it only to agents and
  prompts you would trust at your own keyboard.
- Cookies are read from your local Chrome profile on demand. The server copies the
  (still encrypted) cookie database to a private temp file, reads it, and deletes it
  at once; decrypted cookie values stay in memory only. The Slack `xoxc` token is
  the exception: it is cached in a `0600` file under the cache directory.
- Each connector sends cookies only to its own host.
- Write tools do not exist for the agent until you opt in per instance.
- Large outputs and Confluence backups are written to `~/.cache/chrome-session-mcp/`
  with `0600` permissions. They can contain whatever the tool read: clean the folder
  as you would clean browser downloads.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `no Chrome cookies for <host>` | Open the site in Chrome and log in once. If you use several Chrome profiles, set `chrome_profile`. |
| Keychain prompt on every start | Choose **Always Allow** on the Chrome Safe Storage prompt. |
| `still not logged in after 180s` | Finish the login in the Chrome window that opened (same profile), wait about 30 s for Chrome to save cookies, then retry. |
| Login succeeds but calls are still rejected | You logged in with another account, or in another profile than the one the cookies are read from. `auth_status` shows the profile. |
| `CERTIFICATE_VERIFY_FAILED` | Install `certifi` (`pipx inject chrome-session-mcp certifi`), or add your corporate CA to the system store. |
| Linux: cookies decrypt to garbage | Install `secret-tool` (libsecret-tools), or set `CHROME_SAFE_STORAGE_PASSWORD`. |

## Development

```bash
python3 -m unittest discover -s tests -v      # no network, no Chrome needed
PYTHONPATH=src python3 -m chrome_session_mcp   # run the server from the checkout
```

The MCP stdio transport is newline-delimited JSON-RPC 2.0. The server implements
`initialize`, `tools/list`, `tools/call`, and `ping` directly, and runs each tool call
on a worker thread, so a tool that is waiting for a login does not block `ping`.

Pull requests are welcome, in particular new connector types. A new type needs a
module under `src/chrome_session_mcp/connectors/`, a section in this README, and
unit tests that do not touch the network.

## License

[MIT](LICENSE)

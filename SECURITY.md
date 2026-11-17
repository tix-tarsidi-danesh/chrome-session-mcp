# Security

chrome-session-mcp gives an AI agent **your own logged-in browser sessions**. Anything
you can do in Grafana, Slack, Confluence, Jenkins, and so on, a tool call can do too,
within the tools this server exposes. Read this page before you point it at production systems.

## Threat model

| Risk | What the server does | What you should do |
|------|----------------------|--------------------|
| An agent is steered by a malicious prompt (for example text inside a Slack message, log line, or Confluence page) into an action you did not want | All connectors are read-only by default. Write tools are not even listed until you set `"allow_write": true` on that one instance. Every tool carries MCP `readOnlyHint` / `destructiveHint` annotations so the client can ask before a write. | Turn on `allow_write` only where you need it. Keep your MCP client's per-call approval for write tools. |
| Cookies sent to the wrong site | A connector sends cookies only to its own `base_url` host and refuses absolute URLs on any other host. | Point `base_url` at the exact host. Use `cookie_host` only when you understand which cookies it selects. |
| Cookies or tokens left on disk | Decrypted cookie values stay in memory. The encrypted cookie database is copied to a private temp file and deleted right after reading. The Chrome cookie key is cached in memory for the life of the process. | Treat the cache directory below like a browser profile. |
| Sensitive data in tool output | Large results, Confluence page bodies and backups, and the Slack session token are written under `~/.cache/chrome-session-mcp/` (or `CHROME_SESSION_MCP_CACHE`) with `0600` permissions. | Clean that directory regularly. Do not sync it to cloud storage. |
| Another local user reads your sessions | Not protected against: anyone who can run code as your OS user can already read your Chrome profile. | Same as protecting your browser profile. |

## What this server never does

- Ask for, store, or send passwords. Logins happen in your own Chrome window.
- Send telemetry, or contact any host that is not in your config (plus `slack.com` for the Slack API).
- Bypass a site's own authorization. It can do only what your session is allowed to do.

## Your organization's policies

Many companies restrict automated access to internal tools, or require approval for
tools that act with an employee's credentials. Check with your security team before
you use this server at work, and follow your organization's rules on what data may be
sent to an AI model.

## Reporting a vulnerability

Please report security problems privately through GitHub's
**Security → Report a vulnerability** form on this repository, not in a public issue.
Include the version, your OS, and the steps to reproduce. You should get an answer
within 7 days.

"""chrome-session-mcp: MCP tools over web apps you are logged in to in Google Chrome.

Each connector reuses the cookies of your own Chrome session, so internal tools
behind Google IAP or company SSO work without API tokens. When a session has
expired, the server opens Chrome on the login page, waits for you to log in,
and retries the call.
"""

__version__ = "0.1.0"

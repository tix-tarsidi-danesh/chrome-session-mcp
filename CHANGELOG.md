# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - unreleased

### Added

- MCP stdio server, standard library only (Python 3.9+), no SDK to install.
- Config file with named connector instances; tools appear only for configured types.
- Connectors: `grafana`, `slack`, `jenkins`, `sonarqube`, `confluence`, `kibana`, `http`.
- Built-in tools: `list_connectors`, `auth_status`, `login`, `open_in_browser`.
- Session recovery: an expired session opens Chrome on the login page and the call is retried once the login lands.
- Write tools hidden unless the connector sets `"allow_write": true`; MCP read-only / destructive annotations on every tool.
- Custom connector plugins through `plugin_paths` / `plugins`.
- Confluence write safety: expected-version check, shrink and lost-image/macro guards, automatic backup.

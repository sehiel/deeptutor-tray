# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

Nothing yet.

## [0.9.0] — 2026-11-04

First public release. Replaces the earlier 1.x internal numbering — see
"Why the version is 0.9 and not 1.0" in the README for the reasoning.

### Security

- **Fixed a command injection in the Windows toast path** (`notify.py`).
  Task titles and partner names were interpolated into a double-quoted
  PowerShell here-string, which expands `$(...)`. A session title could
  therefore execute arbitrary commands as the current user. The template now
  uses a single-quoted here-string and every dynamic value travels through an
  environment variable, so no session text is ever parsed as code. The toast
  XML is built with `xml.etree.ElementTree`, which escapes as it serialises.
- **Port clean-up no longer kills unrelated processes** (`launcher.py`).
  It used to terminate any `node.exe` / `python.exe` / `pythonw.exe` /
  `deeptutor.exe` holding port 8001 or 3782 — including a user's own service.
  It now also requires the command line to contain `deeptutor`, and leaves the
  process alone when the command line cannot be read.
- **The local web page validates the `Host` header**, blocking DNS rebinding:
  a hostile page that resolves its own domain to 127.0.0.1 can no longer read
  the per-run token embedded in the launcher page.
- **Toast text is escaped in the web dashboard** (`web/index.html`); partner
  name, avatar URL and colour are no longer interpolated unescaped, and the
  colour must be a literal hex value.

### Fixed

- **The WebSocket observer reconnected every 15 seconds.** `create_connection`'s
  `timeout` also applies to `recv()`, and upstream uvicorn only sends a ping
  frame every 20 seconds — so the connection always timed out before the
  server's keepalive arrived. The log filled with thousands of reconnects and
  completion events arriving during a reconnect were lost. Connect and receive
  timeouts are now separate (`WS_CONNECT_TIMEOUT` / `WS_RECV_TIMEOUT`), and a
  receive timeout pings the connection instead of dropping it.
- **`tasklist` output was decoded as UTF-8**, so the Chinese "no matching
  tasks" notice came back as mojibake on a Chinese Windows. Console output is
  now decoded with a multi-encoding fallback.
- **Two servers could bind the same port.** `http.server` defaults to
  `allow_reuse_address = 1`, which on Windows lets a second process bind a
  port that is already in use — requests then land on either one at random,
  which shows up as "the page shows an old version".
- **`set_state()` was called from several threads without a lock**, so the
  icon state could be written inconsistently.
- **`_shutdown` was never set**, so the watcher and WebSocket threads could
  not wind down on exit.
- **A second launch did not always exit.** The single-instance guard opened
  the browser and then exited, but the browser call could block.

### Added

- **"最近任务" now lists every conversation, not just partner chats.** It
  merges partner chats, partner group chats and main conversations into one
  list sorted by recency, each tagged with its source. Main conversations come
  from a different upstream endpoint with a different shape, so the launcher
  normalises them into a single record.
- **The version number has a single source of truth** (`version.py`). The exe
  filename, the PE version resource, the HTTP `User-Agent` and the dashboard
  footer all follow from `__version__`; `version_info.txt` is generated at
  build time instead of being maintained by hand.
- **Third-party license notices** (`THIRD_PARTY_NOTICES.md`), including the
  LGPL-3.0 obligations that apply to the bundled pystray.

### Changed

- The tray menu is trimmed to four entries — 打开网页 / 最近任务 / 新建任务 /
  退出 — with recent tasks moved into a submenu. Service controls stay on the
  local dashboard.
- Dialogs are drawn by the app (`dialog.py`) instead of using `MessageBox`,
  whose icon control stays 32×32 at 150% DPI and crops the artwork.
- The tray menu no longer closes when the pointer leaves it.
- Build dependencies are pinned to exact versions: the launcher monkey-patches
  pystray's private API, which a newer pystray would break silently.
- `build.bat` regenerates the version resource and reads the artifact name
  from `version.py`; it must stay ASCII-only because `cmd.exe` reads `.bat`
  files using the system code page.

[Unreleased]: https://github.com/sehiel/deeptutor-tray/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/sehiel/deeptutor-tray/releases/tag/v0.9.0

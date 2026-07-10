# Windows support - Design

**Date:** 2026-07-10

## Problem

eufy-sync is macOS only (plus headless Linux for the sync itself). The sync engine, the credential vault, and the Garmin login are already platform-neutral Python; what is macOS-specific is the plumbing around them: launchd for scheduled sync, osascript for notifications, and a handful of doctor checks. Garmin's user base skews Windows, so Windows users are the largest group the "macOS only" line in the README turns away.

Five things stand between the current code and a working Windows release:

1. Auto-sync has no Windows implementation (launchd only).
2. Notifications go through osascript, which does not exist on Windows.
3. Windows Credential Manager caps one stored secret at 2,560 bytes; the single-JSON vault can exceed that once it holds Garmin's two OAuth tokens plus Strava's.
4. Windows locks a running executable, so `--update` replacing eufy-sync while eufy-sync runs it can fail.
5. Doctor, CI, and the README all assume macOS.

## Design

### Platform layer

A new `eufy_sync/platform_support/` package owns everything OS-specific behind one interface:

- `notify(title, message, command=None)` - user notification, click action optional per platform
- `install_agent()` / `uninstall_agent()` - set up or remove scheduled sync
- `agent_status()` - installed / not installed / broken, for doctor and status

Three implementations, selected once at startup from `platform.system()`:

- `macos.py` - the existing launchd and osascript code moves here unchanged in behavior, including the stable-wrapper trick and the terminal-notifier click action.
- `windows.py` - new, described below.
- `generic.py` - no-ops with honest messages ("Auto-sync is not managed on this platform; see the Headless Linux section of the README"). Headless Linux keeps its user-owned systemd timer exactly as today.

`cli/maintenance.py` and `cli/shared.py` shrink accordingly; call sites ask the platform layer instead of branching on OS.

### Windows auto-sync

`--install-agent` creates a per-user Scheduled Task named `eufy-sync` via `schtasks /Create /SC HOURLY /MO 4` (no admin rights needed). The task runs a small VBScript wrapper stored in `~/.garmin-sync/`, which launches `eufy-sync --headless` with the console window hidden and stdout/stderr appended to the existing `sync.log`. Without the wrapper, a console window would flash into focus every 4 hours. As on macOS, the wrapper file's bytes stay identical across releases so the registered task never has to change; it is rewritten only when its content would differ.

`--uninstall-agent` and `--uninstall` remove the task with `schtasks /Delete` and delete the wrapper.

### Windows notifications

`notify()` on Windows sends a native toast through PowerShell's WinRT APIs (built into Windows 10/11, no new dependency). One deliberate difference from macOS: no click-to-run action in v1. Windows toasts cannot start a terminal command without registering a protocol handler, which is more machinery than the feature is worth. The toast text names the fix command instead, which every notification already does. As on macOS, notification failures are swallowed; a lost toast must never break a sync.

### Credential vault chunking

The vault stays one JSON object stored under one logical name. The keychain backend gains transparent chunking: if the serialized vault exceeds a conservative per-entry limit (1,200 characters, safely under Credential Manager's 2,560-byte UTF-16 ceiling), it is split across numbered entries (`vault`, `vault:1`, `vault:2`, ...) and reassembled on read. Writes replace all chunks and delete leftovers from a previously longer vault, so a shrinking vault cannot leave stale tail chunks. The chunking is platform-neutral; macOS keychain entries never hit the limit, so behavior there is unchanged in practice. The 0o600 file fallback stays as the safety net, with one caveat noted in the README: Windows does not honor POSIX file modes, so on Windows the file's protection is the user profile's ACL.

### Self-update on Windows

`--update` on Windows cannot replace `eufy-sync.exe` while it is running. Instead of running the package manager inline, the Windows path launches it in a new detached console that waits two seconds for eufy-sync to exit, then runs the pinned install command (uv, pipx, or pip, same detection as today) and leaves the window open showing the result. macOS and Linux keep the inline path.

### Doctor, CI, docs

- Doctor: the launchd check becomes a platform-layer `agent_status()` check, so Windows gets "scheduled task installed and healthy" with the same PASS/WARN/FAIL reporting.
- CI: `windows-latest` joins the test matrix in `test.yml` alongside ubuntu and macos.
- README: drop "macOS only", add a Windows install section (uv's PowerShell one-liner, mirroring the existing uv path), document the toast behavior and the auto-sync task, keep Headless Linux as is.
- Packaging: add `Operating System :: Microsoft :: Windows` to the pyproject classifiers, which currently list only macOS and Linux.

### First-run offer

The "Set up automatic sync every 4 hours?" prompt after first-run setup is currently gated to macOS. It moves behind the platform layer and fires on Windows too, installing the scheduled task on a yes. The generic platform keeps it silent.

## Non-goals

- No clickable toast actions on Windows in v1.
- No MSI/installer or winget package; install stays pipx/uv.
- No change to sync logic, Garmin auth, Eufy client, or targets.
- No systemd agent management on Linux; the README recipe remains user-owned.

## Testing

Unit tests per platform module with subprocess calls mocked: schtasks create/delete/query argument shapes, the VBScript wrapper's content and rewrite-only-on-change behavior, PowerShell toast invocation, and the generic no-ops. Vault chunking is tested for real (round-trip at sizes below, at, and above the chunk limit; shrink leaves no stale chunks; single-chunk vaults keep today's storage shape so existing installs read cleanly). The full suite runs on Windows CI. Before release, a manual pass on a real Windows machine: install, first-run setup, a real sync, the scheduled task firing with no visible window, a forced failure toast, `--update`, and both uninstall commands. Ships as 1.8.0.

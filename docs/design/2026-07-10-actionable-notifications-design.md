# Actionable macOS notifications - Design

**Date:** 2026-07-10

## Problem

Failure notifications (Garmin re-login, Eufy password change, profile selection, update available) tell the user exactly what to run, but clicking one opens Script Editor. That happens because the tool posts notifications through `osascript`, and macOS attributes those to Script Editor. Plain `osascript` has no way to attach a click action, so the click is a dead end.

## Design

`_notify(title, message)` in `cli/shared.py` gains an optional `command` parameter carrying the fix command the notification already names in its text.

When a command is present and [terminal-notifier](https://github.com/julienXX/terminal-notifier) is installed, the notification goes out through it with an `-execute` action: clicking tells Terminal to open a new window, run the command, and come to the front. The user lands directly in the interactive prompts (Garmin login, password entry, profile picker).

In every other case, behavior is unchanged: no command, no terminal-notifier, or a non-macOS host all take the existing `osascript` path, which fails silently off-platform as before.

terminal-notifier is looked up with `shutil.which` plus the two standard Homebrew locations (`/opt/homebrew/bin`, `/usr/local/bin`), because scheduled runs under launchd get a minimal PATH that may not include Homebrew.

Call sites that gain a command: the three re-auth style failures in `cli/app.py` and the update notice in `cli/updater.py`. Success and generic-failure notifications stay plain; there is nothing useful for a click to do.

## Non-goals

- No new required dependency. terminal-notifier stays optional; the README mentions it in one line.
- No change to notification text or to which events notify.

## Testing

Unit tests on `_notify` with `shutil.which` and `subprocess.run` monkeypatched: the terminal-notifier invocation includes the command inside the `-execute` action; absence of terminal-notifier falls back to `osascript`; a notification without a command uses `osascript` even when terminal-notifier is available.

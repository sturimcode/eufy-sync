# Inline Eufy profile picker and quieter Garmin login output

Date: 2026-06-27
Status: approved, ready for implementation plan

## Problem

Two rough edges showed up in one `eufy-sync` run on 2026-06-27.

1. The account has more than one Eufy profile: an 88 kg human profile and a 4.5 kg
   pet profile. With no profile selected, the sync refuses to guess and fails with
   a dead end. It prints the profiles, tells the user to run
   `eufy-sync --select-profile`, and syncs nothing. The user has to read the
   message, run a second command, then run the sync a third time.

2. The Garmin login printed two WARNING lines about 429 rate limits even though
   login succeeded. The python-garminconnect library tries several login
   strategies in order. `mobile+cffi` and `mobile+requests` each hit a 429 and
   logged a warning, then a later strategy succeeded. The warnings report attempts
   that did not change the result, and they look alarming.

## Goals

- An interactive run that hits profile ambiguity resolves it in place and finishes
  the sync, with no second command.
- A successful Garmin login produces no rate-limit noise on a normal run.
- Neither change hides a genuine failure or a run where no human is present to
  answer a prompt.

## Non-goals

- No heuristic to guess which profile is the human. The user picks once, and the
  choice is remembered.
- No edits to the python-garminconnect library text or its em-dash. That code
  lives in installed site-packages and would be overwritten on the next reinstall.
- No multi-user handling. `load_config` already enforces one user per install.

## Design

### Part 1: Inline profile picker

Resolve `AmbiguousProfileError` in the CLI sync loop (`eufy_sync/cli.py`, the
`except AmbiguousProfileError` block near line 977).

A human is considered present when `not args.headless and sys.stdin.isatty()`.
The `isatty()` check means a scheduled run that forgot `--headless` still will not
hang waiting on input.

When a human is present:

1. Show the profile list and prompt with the existing `_prompt_profile_choice(e.profiles)`.
2. Persist the chosen `customer_id` to the config file through a new shared helper
   `_save_customer_id(config_path, customer_id)`, extracted from the current
   `_select_profile` so the write lives in one place.
3. Set `user.eufy.customer_id` in memory. `EufyConfig` is a mutable dataclass.
4. Re-run `sync_user(user, ...)` once and account the result like a normal success.

When no human is present (`--headless` or no TTY): keep today's behavior. Print the
profile list and the `eufy-sync --select-profile` guidance, record the failure, and
fire a macOS notification with its own clear text instead of the generic
"eufy-sync failed".

Single-user invariant: `load_config` raises if the config holds more than one user,
so the chosen profile always belongs to `users[0]`. The existing `_select_profile`
already relies on this.

### Part 2: Quiet the Garmin 429 retry noise

Extract the logging setup that currently sits inline in the sync path
(`eufy_sync/cli.py:951-958`) into `_configure_logging(verbose: bool)`:

- Root level DEBUG when verbose, WARNING otherwise. Unchanged.
- httpx logger to WARNING. Unchanged.
- New: `garminconnect` logger to ERROR when not verbose.

Call `_configure_logging` from the sync path and the `--reauth` path. Both trigger
a Garmin login and can emit the same noise.

Why ERROR is safe: the library logs each failed strategy at WARNING and only raises
an exception when every strategy fails. That exception propagates to our code, which
reports it through our own message and notification. `sync.py` marks
`GarminConnectTooManyRequestsError` permanent (line 31), so the failure is surfaced
on our terms. Suppressing below ERROR removes the per-strategy noise without hiding
a genuine failure. `--verbose` restores full detail.

## Behavior matrix

| Run type | Profiles ambiguous | Garmin login succeeds after 429s |
| --- | --- | --- |
| Interactive (TTY, no `--headless`) | Prompt, save choice, finish sync | Silent |
| Headless or no TTY | Print guidance, notify, exit non-zero | Silent |
| `--verbose` | Same as above, plus full logs | Full per-strategy logs |

## Tests (TDD)

1. `_save_customer_id` writes the chosen id into the YAML config and leaves the
   other fields intact.
2. Interactive ambiguous-profile path: with `sys.stdin.isatty()` mocked True and
   `input` returning "1", the run saves the choice and retries the sync, producing
   a synced measurement and a success exit.
3. Non-interactive path: with no TTY or `--headless`, `input` is never called, the
   guidance prints, and the run exits non-zero.
4. `_configure_logging(verbose=False)` sets the `garminconnect` logger level to
   ERROR. `_configure_logging(verbose=True)` does not raise it above its default.

## Files touched

- `eufy_sync/cli.py`: inline picker in the sync loop, `_save_customer_id` helper,
  `_configure_logging` helper, dedicated notification text for the headless case.
- `tests/test_cli.py`: the new tests above.

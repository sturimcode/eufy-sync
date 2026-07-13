# Windows test pass report, 2026-07-12

Run on Elias's Windows 11 gaming PC by Claude Code, per the checklist now at docs/checklists/windows-test-pass.md. Build under test: windows-support at commit 1556bb7, version 1.7.22.

## Results

1. PASS. Install. uv was missing; the astral.sh script needs -ExecutionPolicy ByPass, which the Claude Code permission classifier refuses in auto mode, so uv 0.11.28 went in via winget instead. `uv tool install` from the branch succeeded; `eufy-sync --version` prints 1.7.22.
2. PASS. First-run setup. Elias completed it and accepted 4-hourly auto-sync. config.yaml created; sync.log absent because first-run output went to his terminal (allowed case). Open question: the final line was "synced 2 measurements to Strava", not the expected "0 new measurements". Check the Strava account for duplicates of measurements the Mac had already synced; Strava-side dedup is unverified.
3. PASS. Scheduled task listed with status Ready; eufy-sync-agent.vbs present.
4. PASS. Hidden-window run. Triggered via schtasks /Run; sync.log grew 0 to 221 bytes with a completed run; Elias confirmed no console window appeared.
5. PASS, with findings (below). With a wrong stored Garmin password, the scheduled run failed and logged "Authentication failed for default/garmin: Garmin login needed; run: eufy-sync --reauth". Elias saw the toast and it names the fix command. Recovery verified: after restoring the real password, a normal run exits 0 with Garmin and Strava connected.
6. PASS. Doctor: 9 PASS lines including "scheduled task installed, runs every 4h", one acceptable WARN (eufy cloud last weigh-in 2d ago), no FAIL lines, exit 0.
7. PASS. Agent uninstall/reinstall: task gone after uninstall, back with status Ready after reinstall. Ran before step 5 completed (waiting out a suspected rate limit that turned out to be chronic noise); no interaction between the steps.
8. SKIPPED. Self-update, per the checklist (PyPI has no 1.8.0 yet).

## Findings from step 5

- The checklist's injection path did not exist: `--reauth garmin` never prompts for a password while one is stored; it silently reuses it and succeeds. Injection had to go through `--update-password`. The checklist in docs/checklists is updated accordingly.
- The two mobile login strategies in garminconnect return 429 ("IP rate limited by Garmin") on every login, including successful ones. Chronic noise, not rate limiting. They printed as raw warnings on the --update-password path because only --reauth and sync configured logging.
- With a wrong password, the widget strategy raised _MFARequired and the CLI prompted for an MFA code from an email that never arrives. Dead end.
- Ctrl+C at that prompt dumped a raw KeyboardInterrupt traceback.
- Cosmetic: the failure toast is attributed to "Windows PowerShell", and it says `--reauth garmin` where the log says `--reauth`.

## Deviations from the checklist as written

- winget instead of the install script for uv (permission classifier).
- `--update-password` instead of a wrong password at a `--reauth` prompt (no such prompt exists).
- Step 7 ran before step 5 finished. No effect on either.

## What happened after the pass

Elias asked for the step 5 findings to be fixed. PR #53 (merged to windows-support as a828a83) does that:

- Logging is configured once at CLI entry, so every path that reaches a Garmin login quiets the garminconnect 429 warnings, not just --reauth and sync.
- The MFA prompt explains the no-email case; Enter or Ctrl+C cancels with a message naming --update-password.
- Definitively rejected credentials skip the Playwright browser fallback, which would autofill the same bad password and fail minutes later.
- A top-level KeyboardInterrupt handler prints "Cancelled." and exits 130.

327 tests pass locally on Windows including 8 new ones. PR #52's full matrix (ubuntu, macos, windows x 3.12, 3.13) is green with the fix included. The cosmetic toast findings and the Strava dedup question are NOT addressed; they are open for the release session.

# eufy-sync Windows test pass (instructions for Claude Code)

You are Claude Code running on Elias's Windows PC. Your job is to execute this manual test pass for eufy-sync, a published PyPI CLI that syncs Eufy smart scale data to Garmin Connect and Strava. The Windows support you are testing lives on branch `windows-support` of github.com/sturimcode/eufy-sync (PR #52, all CI legs green). This pass is the last gate before it ships as 1.8.0.

Ground rules:

- You EXECUTE the mechanical steps (installs, schtasks commands, reading logs) yourself and record what happened.
- You HAND OFF to Elias for anything interactive or on-screen: typing credentials and the 2FA code, watching whether a console window flashes, confirming a toast notification appeared. Ask him, wait for his answer, record it.
- Do not edit any eufy-sync code, and do not try to fix failures. If a step fails, capture the exact output and move on to whichever later steps still make sense. The fix happens elsewhere; your deliverable is an accurate report.
- Work through the steps in order. Number every result.

## Step 1: Install (you run this)

Check for uv first (`uv --version`). If missing:

```powershell
winget install --id astral-sh.uv --silent
```

(The astral.sh install script needs `-ExecutionPolicy ByPass`, which Claude Code's permission classifier refuses in auto mode; winget installs the same tool without it. Found in the 2026-07-12 pass.)

Then in a shell that can see uv (restart the shell or use the full path uv prints):

```powershell
uv tool install eufy-sync --from git+https://github.com/sturimcode/eufy-sync@windows-support
```

PASS if the install completes and `eufy-sync --version` prints 1.7.22 (the bump to 1.8.0 happens after this pass).

## Step 2: First-run setup (Elias runs this, you verify after)

You cannot drive the interactive setup: it prompts for Eufy and Garmin passwords and possibly a 2FA code, and those are Elias's to type. Ask him to open his own PowerShell window, run `eufy-sync`, complete setup, and say yes to "Set up automatic sync every 4 hours?". Tell him a "0 new measurements" result is a PASS if his Mac already synced today's weigh-in (the Garmin-side dedup is working); a real upload from this PC needs a fresh weigh-in synced here before his Mac's next 4-hourly run.

When he says he is done, verify yourself: `%USERPROFILE%\.garmin-sync\config.yaml` exists, and `type "%USERPROFILE%\.garmin-sync\sync.log"` shows a completed run or is absent only if the first run printed its output to his terminal instead. Record what he reports plus what you can see on disk.

Record the exact per-target counts he reports. The dedup note above covers Garmin only; the 2026-07-12 pass saw "synced 2 measurements to Strava" on first run even though the Mac had already synced, so whether Strava dedups against Mac uploads is an open question. If Strava reports a nonzero count, flag it for the release session to check for duplicates in the Strava account.

## Step 3: Scheduled task exists (you run this)

```powershell
schtasks /Query /TN eufy-sync
```

PASS if one task is listed with status Ready. Also confirm the wrapper exists: `%USERPROFILE%\.garmin-sync\eufy-sync-agent.vbs`.

## Step 4: Hidden-window run (you trigger, Elias watches)

Tell Elias you are about to trigger a background sync and he should watch the screen for any console window flashing, then run:

```powershell
schtasks /Run /TN eufy-sync
```

Wait 30 seconds, then check that sync.log grew (compare its size or tail before and after). PASS if the log shows a new run AND Elias confirms no window appeared. Record both halves separately.

## Step 5: Failure toast (Elias breaks the login, you trigger, Elias watches)

`--reauth garmin` never prompts for a password while one is stored; it silently reuses the saved one and succeeds (found in the 2026-07-12 pass). Break the login through the stored password instead:

1. Ask Elias to run `eufy-sync --update-password`, press Enter to keep the Eufy password, and type a wrong Garmin password on purpose. Nothing on Garmin's side is harmed; only the stored password and local session change.
2. The tool re-authenticates immediately with the wrong password. Expected since PR #53: a clear failure that names `eufy-sync --update-password`. If an MFA code prompt appears instead, no email is coming (a wrong password never generates one); pressing Enter or Ctrl+C cancels it cleanly. Raw 429 warnings ("mobile+cffi returned 429") or a Python traceback on Ctrl+C are regressions; capture them.

When he confirms the login is broken, run:

```powershell
schtasks /Run /TN eufy-sync
```

PASS if Elias sees a Windows toast notification whose text names the fix command (eufy-sync --reauth garmin) AND sync.log records the auth failure. Cosmetic knowns from the 2026-07-12 pass: the toast is attributed to "Windows PowerShell", and the log says `--reauth` where the toast says `--reauth garmin`.

Recovery: he runs `eufy-sync --update-password` again with the real Garmin password (the immediate re-auth should succeed), then you verify a normal `eufy-sync` run exits 0 (it is non-interactive once tokens exist; if it prompts, hand it back to him).

Background for whoever reads the report: the garminconnect library tries five login strategies in order. The two mobile ones are rejected by Garmin with a 429 on every attempt, even when a later strategy succeeds, so a 429 from those paths is noise, not rate limiting caused by the pass. PR #53 quiets them on all CLI paths.

## Step 6: Doctor (you run this)

```powershell
eufy-sync --doctor
```

PASS if there is a "scheduled task" line reporting installed / runs every 4h, and no FAIL lines (a WARN on eufy cloud weigh-in age is acceptable). Capture the full output in your report.

## Step 7: Agent uninstall and reinstall (you run this)

```powershell
eufy-sync --uninstall-agent
schtasks /Query /TN eufy-sync
eufy-sync --install-agent
schtasks /Query /TN eufy-sync
```

PASS if the first query reports the task does not exist and the second shows it back with status Ready.

## Step 8: Self-update (skip)

`--update` compares against PyPI, which does not have 1.8.0 yet, so the detached-window update flow cannot be exercised meaningfully. Record as SKIPPED (verified after release).

## Your report

End with a numbered list, one line per step: PASS / FAIL / SKIPPED plus a short note ("Elias confirmed no window", "toast appeared with reauth command", etc.). For any FAIL, include the exact command output. Elias will paste your report back to the session managing the release.

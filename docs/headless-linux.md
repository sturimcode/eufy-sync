# Headless Linux

eufy-sync can run on a Linux server or VPS, so syncing does not depend on a laptop being awake. Without a working system keychain, it stores credentials in `~/.garmin-sync/credentials.json` with `600` permissions.

## Set up the account

Install eufy-sync with [uv](https://docs.astral.sh/uv/):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv tool install eufy-sync
```

Run `eufy-sync` once in an interactive SSH session. Setup lets you choose Garmin, Strava, and/or experimental Zwift. Garmin login and any two-factor code work in the terminal.

For Strava, forward the authorization callback port when connecting from your computer. Replace `user@server` with your SSH login:

```bash
ssh -L 8089:localhost:8089 user@server
```

Run setup in that session, then open the printed Strava authorization URL in your computer's browser while the command waits. The tunnel sends the browser's localhost callback to eufy-sync on the server.

Run `eufy-sync --doctor` before scheduling it. This catches missing credentials and configuration problems while you still have an interactive prompt.

## Add a systemd user timer

Create `~/.config/systemd/user/eufy-sync.service`:

```ini
[Unit]
Description=eufy-sync

[Service]
Type=oneshot
ExecStart=%h/.local/bin/eufy-sync --headless
```

Create `~/.config/systemd/user/eufy-sync.timer`:

```ini
[Unit]
Description=Run eufy-sync every 4 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=4h

[Install]
WantedBy=timers.target
```

Load and start the timer:

```bash
systemctl --user daemon-reload
systemctl --user enable --now eufy-sync.timer
systemctl --user status eufy-sync.timer
```

A user timer normally runs only while that user's systemd manager is active. If this server must sync after you log out and following a reboot, ask an administrator to enable lingering for the account:

```bash
sudo loginctl enable-linger "$USER"
```

## Check a scheduled run

```bash
systemctl --user start eufy-sync.service
journalctl --user -u eufy-sync.service --since today
```

Scheduled runs normally renew sessions or log back in with stored credentials. A service can still change or reject its login flow, and Garmin may require a security code. When that happens, inspect the journal, run the recovery command it names over SSH, and then start the service again.

Eufy may provide a raw Wi-Fi weight before the phone app processes the full body composition. If a scheduled run sends only weight, open the Eufy app, let it process the weigh-in, and run the service again. More recovery steps are in [Security and troubleshooting](security-and-troubleshooting.md).

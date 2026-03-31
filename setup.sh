#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Route subcommands to eufy-sync CLI
if [ "$1" = "--update" ]; then
    echo "Checking for updates..."
    git fetch origin main --quiet 2>/dev/null
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse origin/main)
    if [ "$LOCAL" = "$REMOTE" ]; then
        echo "Already up to date."
        exit 0
    fi
    echo "Updating..."
    git pull origin main --quiet
    source .venv/bin/activate
    pip install -q -e .
    echo "Updated. Restart any running sync to use the new version."
    exit 0
fi

if [ "$1" = "--update-password" ]; then
    source .venv/bin/activate
    eufy-sync --update-password
    exit 0
fi

if [ "$1" = "--reauth" ]; then
    source .venv/bin/activate
    eufy-sync --reauth
    exit 0
fi

if [ "$1" = "--uninstall" ]; then
    echo "=== Uninstalling eufy-garmin-sync ==="
    echo ""

    if launchctl list 2>/dev/null | grep -q "com.sturimcode.eufy-garmin-sync"; then
        launchctl unload ~/Library/LaunchAgents/com.sturimcode.eufy-garmin-sync.plist 2>/dev/null || true
        echo "Stopped Launch Agent"
    fi
    rm -f ~/Library/LaunchAgents/com.sturimcode.eufy-garmin-sync.plist
    echo "Removed Launch Agent"

    rm -rf ~/.garmin-sync
    echo "Cleared saved tokens and config"

    rm -f /tmp/eufy-garmin-sync.log
    echo "Cleared logs"

    echo ""
    echo "Done. To fully remove: rm -rf $(pwd)"
    exit 0
fi

# Fresh install
echo ""
echo "  eufy-garmin-sync"
echo "  Sync your Eufy scale to Garmin Connect"
echo ""

if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found. Install Python 3.9+ first."
    exit 1
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

echo "Installing dependencies (this may take a minute)..."
source .venv/bin/activate
pip install -q -e .
playwright install chromium > /dev/null 2>&1
echo "Done."

# Run eufy-sync which handles first-run wizard if no config exists
eufy-sync --backfill-days 7

echo ""

# Offer Launch Agent install (macOS only)
if [ "$(uname)" = "Darwin" ]; then
    read -p "Set up automatic sync (every 4 hours + on login)? [y/N] " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        ln -sf "$(pwd)/com.sturimcode.eufy-garmin-sync.plist" ~/Library/LaunchAgents/
        launchctl load ~/Library/LaunchAgents/com.sturimcode.eufy-garmin-sync.plist
        echo "Automatic sync installed. Logs: /tmp/eufy-garmin-sync.log"
    fi
fi

echo ""
echo "You're all set. Check Garmin Connect - your data should be there."

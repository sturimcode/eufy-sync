#!/bin/bash
set -e

# Update mode
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
    pip install -q -r requirements.txt
    echo "Updated. Restart any running sync to use the new version."
    exit 0
fi

# Update passwords mode
if [ "$1" = "--update-password" ]; then
    echo "Update your passwords."
    echo ""
    read -s -p "New Eufy password (press Enter to keep current): " eufy_pw
    echo ""
    read -s -p "New Garmin password (press Enter to keep current): " garmin_pw
    echo ""

    source .env
    [ -n "$eufy_pw" ] && EUFY_PASSWORD="$eufy_pw"
    [ -n "$garmin_pw" ] && GARMIN_PASSWORD="$garmin_pw"

    cat > .env << ENV
EUFY_PASSWORD=$EUFY_PASSWORD
GARMIN_PASSWORD=$GARMIN_PASSWORD
ENV
    chmod 600 .env

    # Clear cached Eufy token so it re-authenticates
    rm -f ~/.garmin-sync/eufy_token.json

    echo "Passwords updated."
    if [ -n "$garmin_pw" ]; then
        echo "Garmin password changed - you'll need to re-login."
        source .venv/bin/activate
        set -a && source .env && set +a
        python -m src.sync --config config.yaml --reauth
    fi
    exit 0
fi

# Reauth mode
if [ "$1" = "--reauth" ]; then
    source .venv/bin/activate
    set -a && source .env && set +a
    python -m src.sync --config config.yaml --reauth
    exit 0
fi

# Uninstall mode
if [ "$1" = "--uninstall" ]; then
    echo "=== Uninstalling eufy-garmin-sync ==="
    echo ""

    # Stop and remove Launch Agent
    if launchctl list 2>/dev/null | grep -q "com.sturimcode.eufy-garmin-sync"; then
        launchctl unload ~/Library/LaunchAgents/com.sturimcode.eufy-garmin-sync.plist 2>/dev/null || true
        echo "Stopped Launch Agent"
    fi
    rm -f ~/Library/LaunchAgents/com.sturimcode.eufy-garmin-sync.plist
    echo "Removed Launch Agent"

    # Clear saved tokens
    rm -rf ~/.garmin-sync
    echo "Cleared saved tokens"

    # Clear logs
    rm -f /tmp/eufy-garmin-sync.log
    echo "Cleared logs"

    echo ""
    echo "Done. The project folder and config files are still here if you want them."
    echo "To fully remove: rm -rf $(pwd)"
    exit 0
fi

echo ""
echo "  eufy-garmin-sync"
echo "  Sync your Eufy scale to Garmin Connect"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found. Install Python 3.9+ first."
    exit 1
fi

# Create venv if needed
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

echo "Installing dependencies (this may take a minute)..."
source .venv/bin/activate
pip install -q -r requirements.txt
playwright install chromium > /dev/null 2>&1
echo "Done."
echo ""

# Collect credentials interactively if config doesn't exist yet
if [ ! -f "config.yaml" ] || [ ! -f ".env" ]; then
    echo "Let's get your accounts connected."
    echo "Credentials are stored locally in .env and only sent to Eufy/Garmin over HTTPS."
    echo ""

    # Eufy email
    read -p "Eufy email: " eufy_email
    if [ -z "$eufy_email" ]; then
        echo "Error: Eufy email is required."
        exit 1
    fi

    # Eufy password
    read -s -p "Eufy password: " eufy_password
    echo ""
    if [ -z "$eufy_password" ]; then
        echo "Error: Eufy password is required."
        exit 1
    fi

    # Garmin email - default to same as Eufy
    read -p "Garmin email (press Enter if same as Eufy): " garmin_email
    if [ -z "$garmin_email" ]; then
        garmin_email="$eufy_email"
    fi

    # Garmin password
    read -s -p "Garmin password: " garmin_password
    echo ""
    if [ -z "$garmin_password" ]; then
        echo "Error: Garmin password is required."
        exit 1
    fi

    # Write config.yaml
    cat > config.yaml << YAML
sync_interval_minutes: 15
log_level: INFO

users:
  - name: "user1"
    eufy:
      email: "$eufy_email"
      password: "\${EUFY_PASSWORD}"
    garmin:
      email: "$garmin_email"
      password: "\${GARMIN_PASSWORD}"
YAML

    # Write .env
    cat > .env << ENV
EUFY_PASSWORD=$eufy_password
GARMIN_PASSWORD=$garmin_password
ENV
    chmod 600 .env

    echo ""
    echo "Credentials saved."
fi

echo ""
echo "Running first sync (last 7 days)..."
echo "A browser window will open for Garmin login - sign in there."
echo ""

set -a && source .env && set +a
python -m src.sync --config config.yaml --backfill-days 7

echo ""

# Offer Launch Agent install (macOS only)
if [ "$(uname)" = "Darwin" ]; then
    read -p "Set up automatic sync (every 4 hours + on login)? [y/N] " answer
    if [[ "$answer" =~ ^[Yy]$ ]]; then
        ln -sf "$(pwd)/com.sturimcode.eufy-garmin-sync.plist" ~/Library/LaunchAgents/
        launchctl load ~/Library/LaunchAgents/com.sturimcode.eufy-garmin-sync.plist
        echo "Automatic sync installed. It runs every 4 hours and when you log in."
        echo "Logs: /tmp/eufy-garmin-sync.log"
    fi
fi

echo ""
echo "You're all set. Check Garmin Connect - your data should be there."

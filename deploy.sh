#!/usr/bin/env bash
# ==============================================================================
# MRTG-CMP - Automated Deployment Script
# Target: Debian 13 (Production Host)
# Pulls updates, syncs dependencies, migrates legacy mrtg-poncab-* systemd units
# to the standardized mrtg-cmp-* units, and verifies all three services.
# ==============================================================================
set -e

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_USER="$(id -un)"
cd "$APP_DIR"

NEW_UNITS=(mrtg-cmp-web mrtg-cmp-collector mrtg-cmp-netcare)
LEGACY_UNITS=(mrtg-poncab mrtg-poncab-web mrtg-poncab-collector)

echo "=========================================================="
echo "🚀 [MRTG-CMP] Deploying latest updates on Debian server"
echo "=========================================================="

echo "📥 1/7 Pulling latest commits from GitHub..."
MAX_ATTEMPTS=3
ATTEMPT=1
until git pull origin master; do
    if [ $ATTEMPT -ge $MAX_ATTEMPTS ]; then
        echo "❌ [DEPLOY ERROR] Git pull failed after $MAX_ATTEMPTS attempts. Deployment aborted."
        exit 1
    fi
    echo "⚠️ Network delay fetching from GitHub. Retrying in 3 seconds ($ATTEMPT/$MAX_ATTEMPTS)..."
    sleep 3
    ATTEMPT=$((ATTEMPT + 1))
done

echo "📦 2/7 Syncing dependencies with uv..."
if command -v uv >/dev/null 2>&1; then
    uv sync
elif [ -x "$HOME/.local/bin/uv" ]; then
    "$HOME/.local/bin/uv" sync
elif [ -x "$APP_DIR/.venv/bin/pip" ]; then
    "$APP_DIR/.venv/bin/pip" install -e .
fi

echo "⚙️ Checking environment configuration (config/.env)..."
CONFIG_DIR="$APP_DIR/config"
CONFIG_ENV="$CONFIG_DIR/.env"
CONFIG_ENV_EXAMPLE="$CONFIG_DIR/.env.example"
mkdir -p "$CONFIG_DIR"
rm -f "$APP_DIR/.env" "$APP_DIR/.env.example"
if [ -f "$CONFIG_ENV" ]; then
    echo "   → using existing config/.env"
elif [ -f "$CONFIG_ENV_EXAMPLE" ]; then
    cp "$CONFIG_ENV_EXAMPLE" "$CONFIG_ENV"
    echo "   → seeded config/.env from config/.env.example"
    echo "   ⚠️  configure your secrets in config/.env before starting services"
fi

echo "🗂️ 3/7 Checking the Netcare target catalog..."
# config/netcare_targets.csv is gitignored: it holds this deployment's own circuit
# IDs, branch names, and facility addresses. A host that has one keeps it (git pull
# never touches untracked files); a fresh host is seeded from the committed
# anonymous example so the dashboard still renders its 18 cards.
CATALOG_DIR="$APP_DIR/config"
CATALOG_EXAMPLE="$CATALOG_DIR/netcare_targets.csv.example"
mkdir -p "$CATALOG_DIR"
if [ -f "$CATALOG_DIR/netcare_targets.csv" ]; then
    echo "   → using local catalog config/netcare_targets.csv"
elif [ -f "$CATALOG_EXAMPLE" ]; then
    cp "$CATALOG_EXAMPLE" "$CATALOG_DIR/netcare_targets.csv"
    echo "   → seeded config/netcare_targets.csv from the committed example"
    echo "   ⚠️  replace its target ids/names with this site's circuits before scraping"
else
    echo "   → no catalog or example present; the built-in anonymous catalog will be used"
fi

echo "🧹 4/7 Stopping and disabling legacy mrtg-poncab-* units (if present)..."
for unit in "${LEGACY_UNITS[@]}"; do
    if systemctl list-unit-files "$unit.service" --no-legend 2>/dev/null | grep -q "$unit.service"; then
        echo "   → disabling legacy $unit.service"
        sudo systemctl disable --now "$unit.service" >/dev/null 2>&1 || true
    else
        echo "   → $unit.service not installed, nothing to retire"
    fi
done
rm -rf "$APP_DIR"/matplotlib-*

echo "📦 5/7 Installing mrtg-cmp-* systemd units..."
sudo systemctl daemon-reload
for unit in "${NEW_UNITS[@]}"; do
    template="$APP_DIR/systemd/$unit.service"
    if [ ! -f "$template" ]; then
        echo "❌ [DEPLOY ERROR] Missing unit template: $template"
        exit 1
    fi
    sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@APP_USER@|$APP_USER|g" \
        "$template" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
    echo "   → installed $unit.service"
done
sudo systemctl daemon-reload

echo "🔄 6/7 Enabling and restarting services (~0.2s)..."
for unit in "${NEW_UNITS[@]}"; do
    sudo systemctl enable "$unit.service" >/dev/null 2>&1 || true
    sudo systemctl restart "$unit.service"
    echo "   → $unit.service restarted"
done

echo "✅ 7/7 Verifying service health..."
for unit in "${NEW_UNITS[@]}"; do
    sudo systemctl status "$unit.service" --no-pager -n 2
done

echo "=========================================================="
echo "🎉 Update complete!"
echo "   Web dashboard : http://<host>:8000"
echo "   Netcare cache : $APP_DIR/data/netcare_cache"
echo "=========================================================="

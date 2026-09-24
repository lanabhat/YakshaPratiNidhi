#!/bin/bash
# Run this FROM a PythonAnywhere Bash console, inside this repo's checkout there.
# Pulls the latest code pushed from your local machine and reloads the live web app -
# one direction only: PythonAnywhere never pushes back to GitHub, it only catches up.
#
# Auto-reload needs a PythonAnywhere API token (Account -> "API Token" -> create one)
# saved as PYTHONANYWHERE_API_TOKEN in THIS machine's .env (the one on PythonAnywhere,
# not your local one). Without it, this script still pulls - it just reminds you to
# reload by hand via the Web tab instead of doing it for you.
set -euo pipefail
cd "$(dirname "$0")"

echo "== git pull =="
git pull

if [ ! -f .env ]; then
    echo "No .env here - can't look up API_BASE_URL/PYTHONANYWHERE_API_TOKEN. Reload manually via the Web tab."
    exit 0
fi

API_BASE_URL=$(grep -E '^API_BASE_URL=' .env | head -n1 | cut -d= -f2-)
API_TOKEN=$(grep -E '^PYTHONANYWHERE_API_TOKEN=' .env | head -n1 | cut -d= -f2-)

if [ -z "$API_TOKEN" ]; then
    echo "PYTHONANYWHERE_API_TOKEN not set in .env - skipping auto-reload."
    echo "Reload manually: PythonAnywhere dashboard -> Web tab -> Reload."
    exit 0
fi

if [ -z "$API_BASE_URL" ]; then
    echo "API_BASE_URL not set in .env - can't tell which app to reload. Reload manually via the Web tab."
    exit 0
fi

DOMAIN=$(echo "$API_BASE_URL" | sed -E 's#^https?://##; s#/.*##')
USERNAME=$(echo "$DOMAIN" | cut -d. -f1)

echo "== reloading $DOMAIN =="
curl -sS -X POST \
    -H "Authorization: Token $API_TOKEN" \
    "https://www.pythonanywhere.com/api/v0/user/$USERNAME/webapps/$DOMAIN/reload/"
echo

echo "== health check =="
sleep 3
curl -sS "$API_BASE_URL/api/health"
echo
echo "== done =="

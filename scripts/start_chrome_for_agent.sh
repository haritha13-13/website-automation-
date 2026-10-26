#!/usr/bin/env bash
# Start a Chrome that YOU control, with a separate profile, so you can sign in yourself (Google, MFA, CAPTCHA...)
# and let the agent attach to it:   python main.py --attach-chrome
#
# The browser listens for local connections only (127.0.0.1:PORT). While it is open, programs on this
# machine can control it, so close it when you are done. Your normal Chrome profile is not touched.
set -euo pipefail
PORT="${1:-9222}"
PROFILE="${AGENT_CHROME_PROFILE:-$HOME/.agent-chrome-profile}"
CHROME="${CHROME_BIN:-}"
if [ -z "$CHROME" ]; then
  for c in google-chrome google-chrome-stable chromium chromium-browser; do
    if command -v "$c" >/dev/null 2>&1; then CHROME="$(command -v "$c")"; break; fi
  done
fi
if [ -z "$CHROME" ]; then  # fall back to the Chromium that Playwright downloaded
  CHROME="$(ls -d "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null | sort | tail -n 1 || true)"
  if [ -n "$CHROME" ]; then
    echo "WARNING: Google Chrome is not installed, so this uses Playwright's test Chromium." >&2
    echo "         Google will refuse to sign in there ('This browser or app may not be secure')." >&2
    echo "         Install real Chrome:  cd /tmp && wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb" >&2
    echo "                               sudo apt install ./google-chrome-stable_current_amd64.deb" >&2
  fi
fi
if [ -z "$CHROME" ] || [ ! -x "$CHROME" ]; then
  echo "No Chrome/Chromium found. Install Google Chrome, or set CHROME_BIN=/path/to/chrome." >&2
  exit 1
fi
mkdir -p "$PROFILE"
echo "Starting: $CHROME"
echo "Profile : $PROFILE   Debug port: $PORT (local only)"
echo "Sign in to the website in this window, then run:  python main.py --attach-chrome $PORT"
exec "$CHROME" --remote-debugging-port="$PORT" --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$PROFILE" --no-first-run --no-default-browser-check about:blank

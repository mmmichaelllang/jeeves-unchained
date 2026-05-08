#!/usr/bin/env bash
#
# scripts/recover_oauth.sh — one-shot Gmail OAuth refresh-token recovery.
#
# When the daily pipeline fails with "invalid_grant: Token has been expired
# or revoked", run this script to:
#
#   1. Mint a fresh token.json via scripts/gmail_auth.py (browser consent).
#   2. Validate the token by exercising a refresh (scripts/check_gmail_oauth.py).
#   3. Upload the token JSON to the GitHub secret GMAIL_OAUTH_TOKEN_JSON.
#   4. Wipe the local token.json (keeps repo clean of credentials).
#   5. Optionally trigger daily.yml workflow_dispatch for today.
#
# Prerequisites:
#   - gh CLI installed AND authenticated against mmmichaelllang/jeeves-unchained
#     (run `gh auth status` first).
#   - python3 with the project venv (handled automatically via uv if available).
#   - ~/Downloads/credentials.json (Desktop OAuth client JSON from Google
#     Cloud Console). Override the path with --credentials.
#
# Usage:
#   ./scripts/recover_oauth.sh
#   ./scripts/recover_oauth.sh --credentials /path/to/credentials.json
#   ./scripts/recover_oauth.sh --no-trigger
#   ./scripts/recover_oauth.sh --repo owner/repo
#

set -euo pipefail

# ---- defaults --------------------------------------------------------------
DEFAULT_CREDENTIALS="${HOME}/Downloads/credentials.json"
DEFAULT_REPO="mmmichaelllang/jeeves-unchained"
SECRET_NAME="GMAIL_OAUTH_TOKEN_JSON"

CREDENTIALS_PATH="${DEFAULT_CREDENTIALS}"
REPO="${DEFAULT_REPO}"
TRIGGER_DAILY=1
SKIP_VERIFY=0

usage() {
  sed -n '2,30p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

# ---- arg parsing -----------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --credentials) CREDENTIALS_PATH="$2"; shift 2 ;;
    --repo)        REPO="$2"; shift 2 ;;
    --no-trigger)  TRIGGER_DAILY=0; shift ;;
    --skip-verify) SKIP_VERIFY=1; shift ;;
    -h|--help)     usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 2 ;;
  esac
done

# ---- locate repo root ------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

# Token always written here; cleaned up at the end.
TOKEN_PATH="${REPO_ROOT}/.gmail-token.json"

# ---- helpers ---------------------------------------------------------------
red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

step() { echo; bold "==> $*"; }

cleanup() {
  if [[ -f "${TOKEN_PATH}" ]]; then
    rm -f "${TOKEN_PATH}"
    yellow "cleaned up ${TOKEN_PATH}"
  fi
}
trap cleanup EXIT

# Pick `uv run` if available, else plain python3 — gmail_auth.py needs
# google-auth-oauthlib which is in the project's pyproject extras.
run_python() {
  if command -v uv >/dev/null 2>&1; then
    uv run python "$@"
  else
    python3 "$@"
  fi
}

# ---- preflight -------------------------------------------------------------
step "Preflight"

if ! command -v gh >/dev/null 2>&1; then
  red "gh CLI not found. Install: https://cli.github.com/"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  red "gh CLI is not authenticated. Run: gh auth login"
  exit 1
fi

if [[ ! -f "${CREDENTIALS_PATH}" ]]; then
  red "credentials.json not found at: ${CREDENTIALS_PATH}"
  echo
  echo "Download a Desktop OAuth 2.0 Client JSON from:"
  echo "  https://console.cloud.google.com/apis/credentials"
  echo "Save it (default path: ~/Downloads/credentials.json) or pass"
  echo "--credentials /path/to/file.json."
  exit 1
fi

if [[ ! -f scripts/gmail_auth.py ]]; then
  red "scripts/gmail_auth.py not found — wrong working directory?"
  exit 1
fi

green "ok — gh authed, credentials.json present, repo root resolved (${REPO_ROOT})"

# ---- mint token ------------------------------------------------------------
step "Mint a fresh OAuth token (a browser tab will open for consent)"
echo "Credentials: ${CREDENTIALS_PATH}"
echo "Output:      ${TOKEN_PATH}"
echo

run_python scripts/gmail_auth.py \
  --credentials "${CREDENTIALS_PATH}" \
  --out "${TOKEN_PATH}"

if [[ ! -s "${TOKEN_PATH}" ]]; then
  red "scripts/gmail_auth.py produced no token at ${TOKEN_PATH}"
  exit 1
fi

# Sanity-check shape — must contain a refresh_token, otherwise Google has
# returned a one-shot access token (e.g., "access_type=online" was used).
if ! grep -q '"refresh_token"' "${TOKEN_PATH}"; then
  red "token.json has no refresh_token field — re-issue the OAuth client"
  red "with access_type=offline and re-run this script."
  exit 1
fi

green "token minted (with refresh_token)"

# ---- verify locally --------------------------------------------------------
if [[ "${SKIP_VERIFY}" -eq 0 ]]; then
  step "Verify token by exercising a refresh"
  GMAIL_OAUTH_TOKEN_JSON="$(cat "${TOKEN_PATH}")" \
    run_python scripts/check_gmail_oauth.py --no-alert --quiet
  green "refresh ok"
else
  yellow "--skip-verify set; skipping local refresh test"
fi

# ---- push to GitHub secret -------------------------------------------------
step "Update GitHub secret ${SECRET_NAME} on ${REPO}"
# `gh secret set` reads value from stdin when --body is omitted.
gh secret set "${SECRET_NAME}" --repo "${REPO}" < "${TOKEN_PATH}"
green "secret ${SECRET_NAME} updated on ${REPO}"

# ---- optionally trigger today's pipeline -----------------------------------
if [[ "${TRIGGER_DAILY}" -eq 1 ]]; then
  step "Trigger daily.yml workflow_dispatch"
  TODAY="$(date -u +%Y-%m-%d)"
  echo "Dispatching daily.yml for date=${TODAY}"
  if gh workflow run daily.yml \
       --repo "${REPO}" \
       --ref main \
       -f "date=${TODAY}" >/dev/null 2>&1; then
    green "daily.yml dispatched for ${TODAY} (branch=main)"
    echo "Watch: gh run watch --repo ${REPO}"
  else
    yellow "could not dispatch daily.yml — trigger manually in GitHub UI:"
    yellow "  https://github.com/${REPO}/actions/workflows/daily.yml"
  fi
else
  yellow "--no-trigger set; skipping daily.yml dispatch"
fi

step "Done"
green "OAuth recovered. Today's pipeline is queued (or trigger it manually)."
echo
echo "Recommended follow-up:"
echo "  1. Confirm the OAuth consent screen is set to 'In production' in"
echo "     Google Cloud Console (APIs & Services -> OAuth consent screen)."
echo "     'Testing' mode revokes refresh tokens after 7 days."
echo "  2. Watch the run: gh run watch --repo ${REPO}"

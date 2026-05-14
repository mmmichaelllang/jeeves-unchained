#!/bin/bash
# Runner for probe_agent_path.py. Exercises full jeeves stack against
# real NIM + search APIs for one sector.
set -u
cd /Users/frederickyudin/jeeves-unchained
echo "=== start: $(date -u +%FT%TZ) ==="
echo "=== cwd: $(pwd) ==="
source .venv/bin/activate
echo "=== python: $(which python3) ==="
python3 --version
set -a
source .env
set +a
# Fill GITHUB_TOKEN from gh CLI keychain (non-TTY safe per TK memory)
if [ -z "${GITHUB_TOKEN:-}" ] || [ "${GITHUB_TOKEN:0:4}" = "    " ]; then
    GH=""
    [ -x /opt/homebrew/bin/gh ] && GH=/opt/homebrew/bin/gh
    [ -z "$GH" ] && [ -x /usr/local/bin/gh ] && GH=/usr/local/bin/gh
    [ -z "$GH" ] && GH=$(command -v gh 2>/dev/null)
    if [ -n "$GH" ]; then
        TOK=$("$GH" auth token 2>/dev/null)
        if [ -n "$TOK" ]; then
            export GITHUB_TOKEN="$TOK"
            echo "=== GITHUB_TOKEN sourced from gh keychain (len=${#GITHUB_TOKEN}) ==="
        else
            echo "=== gh auth token returned empty ==="
        fi
    else
        echo "=== gh CLI not found at expected paths ==="
    fi
fi
SECTOR="${1:-local_news}"
PROBE="${2:-probe_agent_path_v2.py}"
echo "=== sector: ${SECTOR} ==="
echo "=== probe: ${PROBE} ==="
# cwd is repo root (set above) so jeeves.* imports resolve.
python3 "scripts/diagnostics/${PROBE}" "${SECTOR}" 2>&1
EXIT=$?
echo "=== end: $(date -u +%FT%TZ)  exit_code=${EXIT} ==="

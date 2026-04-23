#!/usr/bin/env python3
"""One-time local Gmail OAuth bootstrap.

Run this once on your laptop with `credentials.json` (downloaded from
Google Cloud Console → OAuth 2.0 Client IDs → Desktop app) in the working
directory. It will:

  1. Open a browser for consent,
  2. Print the resulting token.json contents to stdout,
  3. Save token.json locally.

Paste the printed JSON into the GitHub secret GMAIL_OAUTH_TOKEN_JSON.
Workflows use the stored refresh token to obtain fresh access tokens at
runtime — no further interactive auth is needed.

Usage:
  python scripts/gmail_auth.py --credentials ~/Downloads/credentials.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Gmail OAuth bootstrap.")
    ap.add_argument(
        "--credentials",
        default="credentials.json",
        help="Path to OAuth 2.0 client-secret JSON from Google Cloud Console.",
    )
    ap.add_argument(
        "--out",
        default="token.json",
        help="Where to write the resulting user token (default token.json).",
    )
    args = ap.parse_args()

    from google_auth_oauthlib.flow import InstalledAppFlow

    client_path = Path(args.credentials).expanduser()
    if not client_path.exists():
        print(
            f"ERROR: {client_path} not found. Download a Desktop OAuth 2.0 client JSON from "
            "https://console.cloud.google.com/apis/credentials and pass it via --credentials.",
            file=sys.stderr,
        )
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_json = creds.to_json()

    out_path = Path(args.out).expanduser()
    out_path.write_text(token_json, encoding="utf-8")

    print(
        "\n=== SUCCESS ==="
        "\n\nPaste the JSON below into the GitHub secret GMAIL_OAUTH_TOKEN_JSON"
        " (Settings → Secrets and variables → Actions → New repository secret):\n",
        file=sys.stderr,
    )
    # Pretty-print to stdout so `| tee` captures it cleanly.
    print(json.dumps(json.loads(token_json), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

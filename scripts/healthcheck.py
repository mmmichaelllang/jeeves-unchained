#!/usr/bin/env python3
"""Jeeves pipeline health check.

Validates all required secrets and API connectivity for each phase without
running the full pipeline.  Exits 0 if all checks pass; exits 1 otherwise.

Usage:
    python scripts/healthcheck.py                # full check (makes test calls)
    python scripts/healthcheck.py --dry-run      # secrets only, no network calls
    python scripts/healthcheck.py --phase write  # check one phase only
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

# Allow running directly from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

PASS = "✅ PASS"
FAIL = "❌ FAIL"
SKIP = "⚠️  SKIP"

_results: list[tuple[str, str, str]] = []  # (check, status, detail)


def _record(check: str, ok: bool | None, detail: str = "") -> bool | None:
    if ok is None:
        _results.append((check, SKIP, detail))
    else:
        _results.append((check, PASS if ok else FAIL, detail))
    return ok


# ---------------------------------------------------------------------------
# Secret checks
# ---------------------------------------------------------------------------

def _check_secret(name: str) -> bool:
    val = os.environ.get(name, "")
    ok = bool(val)
    _record(f"secret:{name}", ok, "" if ok else "NOT SET")
    return ok


SECRETS_BY_PHASE: dict[str, list[str]] = {
    "research": [
        "NVIDIA_API_KEY",
        "SERPER_API_KEY",
        "TAVILY_API_KEY",
        "EXA_API_KEY",
        "GOOGLE_API_KEY",
        "GITHUB_TOKEN",
    ],
    "write": [
        "GROQ_API_KEY",
        "GMAIL_APP_PASSWORD",
    ],
    "correspondence": [
        "NVIDIA_API_KEY",
        "GROQ_API_KEY",
        "GMAIL_OAUTH_TOKEN_JSON",
        "GMAIL_APP_PASSWORD",
    ],
}
OPTIONAL_SECRETS = ["OPENROUTER_API_KEY", "JINA_API_KEY", "FIRECRAWL_API_KEY"]


def check_secrets(phases: list[str]) -> bool:
    all_required: set[str] = set()
    for phase in phases:
        all_required.update(SECRETS_BY_PHASE.get(phase, []))
    results = [_check_secret(s) for s in sorted(all_required)]
    for s in OPTIONAL_SECRETS:
        val = os.environ.get(s, "")
        _record(f"optional:{s}", None, "set" if val else "not set")
    return all(results)


# ---------------------------------------------------------------------------
# API connectivity checks
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict, *, timeout: int = 10) -> tuple[int, str]:
    try:
        r = httpx.get(url, headers=headers, timeout=timeout)
        return r.status_code, r.text[:120]
    except Exception as exc:
        return -1, str(exc)[:120]


def _post(url: str, headers: dict, json: dict, *, timeout: int = 15) -> tuple[int, str]:
    try:
        r = httpx.post(url, headers=headers, json=json, timeout=timeout)
        return r.status_code, r.text[:200]
    except Exception as exc:
        return -1, str(exc)[:120]


def check_serper() -> bool:
    key = os.environ.get("SERPER_API_KEY", "")
    if not key:
        _record("api:serper", None, "no key")
        return True
    code, body = _post(
        "https://google.serper.dev/search",
        {"X-API-KEY": key, "Content-Type": "application/json"},
        {"q": "jeeves healthcheck", "num": 1},
    )
    ok = code == 200
    _record("api:serper", ok, f"HTTP {code}" if not ok else "")
    return ok


def check_groq() -> bool:
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        _record("api:groq", None, "no key")
        return True
    code, body = _post(
        "https://api.groq.com/openai/v1/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        },
    )
    ok = code in (200, 429)  # 429 = key valid but rate-limited
    _record("api:groq", ok, f"HTTP {code}" if not ok else ("rate-limited" if code == 429 else ""))
    return ok


def check_nim() -> bool:
    key = os.environ.get("NVIDIA_API_KEY", "")
    if not key:
        _record("api:nim", None, "no key")
        return True
    code, body = _post(
        "https://integrate.api.nvidia.com/v1/chat/completions",
        {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        {
            "model": "moonshotai/kimi-k2.6",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        },
    )
    ok = code in (200, 429)
    _record("api:nim", ok, f"HTTP {code}" if not ok else ("rate-limited" if code == 429 else ""))
    return ok


def check_tavily() -> bool:
    key = os.environ.get("TAVILY_API_KEY", "")
    if not key:
        _record("api:tavily", None, "no key")
        return True
    code, body = _post(
        "https://api.tavily.com/search",
        {"Content-Type": "application/json"},
        {"api_key": key, "query": "jeeves healthcheck", "max_results": 1},
    )
    ok = code == 200
    _record("api:tavily", ok, f"HTTP {code}" if not ok else "")
    return ok


def check_exa() -> bool:
    key = os.environ.get("EXA_API_KEY", "")
    if not key:
        _record("api:exa", None, "no key")
        return True
    code, body = _post(
        "https://api.exa.ai/search",
        {"x-api-key": key, "Content-Type": "application/json"},
        {"query": "jeeves healthcheck", "numResults": 1},
    )
    ok = code == 200
    _record("api:exa", ok, f"HTTP {code}" if not ok else "")
    return ok


def check_openrouter() -> bool:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        _record("api:openrouter", None, "no key (optional)")
        return True
    code, body = _get(
        "https://openrouter.ai/api/v1/models",
        {"Authorization": f"Bearer {key}"},
    )
    ok = code == 200
    _record("api:openrouter", ok, f"HTTP {code}" if not ok else "")
    return ok


# ---------------------------------------------------------------------------
# Local file / config checks
# ---------------------------------------------------------------------------

def check_local_files() -> bool:
    repo_root = Path(__file__).resolve().parent.parent
    required = [
        repo_root / "jeeves" / "prompts" / "write_system.md",
        repo_root / "jeeves" / "prompts" / "research_system.md",
        repo_root / "jeeves" / "prompts" / "email_scaffold.html",
        repo_root / "jeeves" / "config.py",
        repo_root / "jeeves" / "schema.py",
    ]
    all_ok = True
    for p in required:
        ok = p.exists()
        _record(f"file:{p.name}", ok, "" if ok else "MISSING")
        all_ok = all_ok and ok
    return all_ok


def check_quota_state() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    qpath = repo_root / ".quota-state.json"
    if qpath.exists():
        _record("quota-state.json", None, f"exists ({qpath.stat().st_size} bytes)")
    else:
        _record("quota-state.json", None, "not present (will be created on first run)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Jeeves pipeline health check")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Check secrets only; skip API connectivity calls",
    )
    parser.add_argument(
        "--phase", choices=["research", "write", "correspondence", "all"],
        default="all",
        help="Which phase to check (default: all)",
    )
    args = parser.parse_args()

    phases = list(SECRETS_BY_PHASE.keys()) if args.phase == "all" else [args.phase]

    print(f"\n{'=' * 60}")
    print(f"  Jeeves Health Check  |  {date.today()}  |  phases: {', '.join(phases)}")
    print(f"{'=' * 60}\n")

    secrets_ok = check_secrets(phases)
    check_local_files()
    check_quota_state()

    if not args.dry_run:
        print("\n[API connectivity — making minimal test calls...]\n")
        if "research" in phases:
            check_serper()
            check_tavily()
            check_exa()
            check_nim()
        if "write" in phases:
            check_groq()
        check_openrouter()

    # --- Print table ---
    col_w = (38, 10, 0)
    print(f"\n{'─' * 60}")
    print(f"  {'Check':<38} {'Status':<10} Detail")
    print(f"{'─' * 60}")
    any_fail = False
    for check, status, detail in _results:
        print(f"  {check:<38} {status:<10} {detail}")
        if status == FAIL:
            any_fail = True
    print(f"{'─' * 60}")

    if any_fail:
        print("\n❌  One or more checks FAILED.\n")
        return 1
    print("\n✅  All required checks passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

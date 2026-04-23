"""Load/save session JSON files — local disk + GitHub commit."""

from __future__ import annotations

import base64
import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .schema import SessionModel, apply_field_caps

log = logging.getLogger(__name__)


def load_previous_session(cfg: Config) -> SessionModel | None:
    """Look back up to 7 days for the most recent session file on disk."""

    for delta in range(1, 8):
        path = cfg.session_path(cfg.run_date - timedelta(days=delta))
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return SessionModel.model_validate(data)
            except Exception as e:
                log.warning("failed to parse prior session %s: %s", path, e)
    return None


def _write_local(path: Path, session: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def save_session(session: dict[str, Any], cfg: Config) -> Path:
    """Validate, cap fields, write locally, and (unless dry-run) commit to GitHub."""

    apply_field_caps(session)
    SessionModel.model_validate(session)

    local_name = (
        f"session-{cfg.run_date.isoformat()}.json"
        if not cfg.dry_run
        else f"session-{cfg.run_date.isoformat()}.local.json"
    )
    path = cfg.sessions_dir / local_name
    _write_local(path, session)
    log.info("session written to %s", path)

    if cfg.dry_run:
        return path

    _commit_to_github(path, cfg)
    return path


def _commit_to_github(path: Path, cfg: Config) -> None:
    """Create-or-update the session file in the jeeves-unchained repo."""

    repo = cfg.github_repository
    rel = f"sessions/{path.name}"
    api = f"https://api.github.com/repos/{repo}/contents/{rel}"
    headers = {
        "Authorization": f"token {cfg.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    existing_sha: str | None = None
    with httpx.Client(timeout=30.0) as client:
        r = client.get(api, headers=headers)
        if r.status_code == 200:
            existing_sha = r.json().get("sha")
        elif r.status_code not in (404,):
            r.raise_for_status()

        content_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        payload: dict[str, Any] = {
            "message": f"jeeves research {cfg.run_date.isoformat()}",
            "content": content_b64,
            "branch": _current_branch(cfg),
        }
        if existing_sha:
            payload["sha"] = existing_sha

        put = client.put(api, headers=headers, json=payload)
        put.raise_for_status()
        log.info("committed %s to %s", rel, repo)


def _current_branch(cfg: Config) -> str:
    """Resolve the branch the workflow is running on.

    In GitHub Actions `GITHUB_REF_NAME` points at the branch. Locally we
    default to `main` — callers can override via env.
    """

    import os

    return (
        os.environ.get("GITHUB_REF_NAME")
        or os.environ.get("JEEVES_TARGET_BRANCH")
        or "main"
    )


def load_session_by_date(cfg: Config, d: date) -> SessionModel:
    path = cfg.session_path(d)
    return SessionModel.model_validate(json.loads(path.read_text(encoding="utf-8")))

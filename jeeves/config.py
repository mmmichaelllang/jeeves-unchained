"""Environment configuration — loaded once at startup.

`Config.from_env()` collects every secret name needed and raises
`MissingSecret` listing ALL gaps at once rather than discovering them
one at a time at the first network call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv


class MissingSecret(RuntimeError):
    def __init__(self, names: list[str]):
        super().__init__(
            "Missing required environment variables: " + ", ".join(names)
        )
        self.names = names


@dataclass
class Config:
    # Research-phase secrets
    nvidia_api_key: str
    serper_api_key: str
    tavily_api_key: str
    exa_api_key: str
    google_api_key: str
    # Shared
    github_token: str
    github_repository: str
    # Runtime
    run_date: date
    dry_run: bool = False
    verbose: bool = False
    # Model IDs
    kimi_model_id: str = "moonshotai/kimi-k2-instruct"
    kimi_base_url: str = "https://integrate.api.nvidia.com/v1"
    groq_model_id: str = "llama-3.3-70b-versatile"
    # Recipient
    recipient_email: str = "lang.mc@gmail.com"
    # Paths
    repo_root: Path = field(default_factory=lambda: _default_repo_root())

    @property
    def sessions_dir(self) -> Path:
        return self.repo_root / "sessions"

    @property
    def quota_state_path(self) -> Path:
        return self.repo_root / ".quota-state.json"

    def session_path(self, d: date | None = None) -> Path:
        target = d or self.run_date
        return self.sessions_dir / f"session-{target.isoformat()}.json"

    @classmethod
    def from_env(
        cls,
        *,
        dry_run: bool = False,
        run_date: date | str | None = None,
        verbose: bool = False,
    ) -> Config:
        load_dotenv()

        resolved_date = _parse_date(run_date)

        # In dry-run we tolerate missing network keys (mocks replace them).
        required = [
            "GITHUB_REPOSITORY",
        ]
        if not dry_run:
            required += [
                "NVIDIA_API_KEY",
                "SERPER_API_KEY",
                "TAVILY_API_KEY",
                "EXA_API_KEY",
                "GOOGLE_API_KEY",
                "GITHUB_TOKEN",
            ]

        missing = [n for n in required if not os.environ.get(n)]
        if missing:
            raise MissingSecret(missing)

        return cls(
            nvidia_api_key=os.environ.get("NVIDIA_API_KEY", ""),
            serper_api_key=os.environ.get("SERPER_API_KEY", ""),
            tavily_api_key=os.environ.get("TAVILY_API_KEY", ""),
            exa_api_key=os.environ.get("EXA_API_KEY", ""),
            google_api_key=os.environ.get("GOOGLE_API_KEY", ""),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            github_repository=os.environ.get(
                "GITHUB_REPOSITORY", "mmmichaelllang/jeeves-unchained"
            ),
            run_date=resolved_date,
            dry_run=dry_run,
            verbose=verbose,
            kimi_model_id=os.environ.get("KIMI_MODEL_ID", "moonshotai/kimi-k2-instruct"),
            kimi_base_url=os.environ.get(
                "KIMI_BASE_URL", "https://integrate.api.nvidia.com/v1"
            ),
            groq_model_id=os.environ.get("GROQ_MODEL_ID", "llama-3.3-70b-versatile"),
            recipient_email=os.environ.get("JEEVES_RECIPIENT_EMAIL", "lang.mc@gmail.com"),
        )


def _default_repo_root() -> Path:
    """Resolve the repo root.

    Priority: JEEVES_REPO_ROOT env override (tests) → GITHUB_WORKSPACE (CI)
    → the directory containing this file's parent package.
    """

    override = os.environ.get("JEEVES_REPO_ROOT")
    if override:
        return Path(override).resolve()
    ws = os.environ.get("GITHUB_WORKSPACE")
    if ws:
        return Path(ws).resolve()
    return Path(__file__).resolve().parent.parent


def _parse_date(value: date | str | None) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    return datetime.now(timezone.utc).date()

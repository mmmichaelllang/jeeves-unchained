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
from typing import Literal

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Write-phase dedup prompt caps — shared source of truth; imported by write.py
# ---------------------------------------------------------------------------
DEDUP_PROMPT_HEADLINES_CAP: int = 150   # max prior headlines sent to Groq (sprint-17: was 250 — burned 12k TPM)
DEDUP_PROMPT_ASIDES_CAP: int = 20       # max aside phrases in Part 4+ prompt
DEDUP_PROMPT_TOPICS_CAP: int = 60       # max used topics in Part 4+ prompt (sprint-17: was 80)

# ---------------------------------------------------------------------------
# Research phase tool budgets — injected into per-sector user messages
# ---------------------------------------------------------------------------
RESEARCH_BUDGET_TAVILY_SEARCH: int = 4
RESEARCH_BUDGET_TAVILY_EXTRACT: int = 5   # URLs total (20 hits max)
RESEARCH_BUDGET_GEMINI: int = 3
RESEARCH_BUDGET_EXA: int = 7             # 1 reserved for literary_pick
RESEARCH_BUDGET_SERPER: int = 20
RESEARCH_BUDGET_PLAYWRIGHT: int = 5      # last-resort; each call ~5-15s
# Sprint-19 search-agent canaries — opt-in via JEEVES_USE_* flags. Budgets
# only bind when the corresponding tool is registered.
RESEARCH_BUDGET_JINA_SEARCH: int = 10
RESEARCH_BUDGET_JINA_DEEPSEARCH: int = 3      # token-heavy; 1 call/deep sector
RESEARCH_BUDGET_JINA_RERANK: int = 8
RESEARCH_BUDGET_TINYFISH_SEARCH: int = 8
RESEARCH_BUDGET_PLAYWRIGHT_SEARCH: int = 20   # zero-API; bounded by wall-clock

Phase = Literal["research", "write", "correspondence"]

# Per-phase required environment variables (in addition to shared ones).
PHASE_REQUIREMENTS: dict[Phase, list[str]] = {
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

SHARED_REQUIRED = ["GITHUB_REPOSITORY"]


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
    # Write-phase secrets
    groq_api_key: str
    gmail_app_password: str
    # Correspondence-phase secrets
    gmail_oauth_token_json: str
    # Shared
    github_token: str
    github_repository: str
    # Runtime
    run_date: date
    dry_run: bool = False
    verbose: bool = False
    phase: Phase = "research"
    # Model IDs
    kimi_model_id: str = "moonshotai/kimi-k2-instruct"
    kimi_base_url: str = "https://integrate.api.nvidia.com/v1"
    groq_model_id: str = "llama-3.3-70b-versatile"
    nim_write_model_id: str = "meta/llama-3.3-70b-instruct"
    # Jina AI reader (optional — used by talk_of_the_town for clean markdown)
    jina_api_key: str = ""
    # OpenRouter (optional — narrative editor pass in write phase)
    openrouter_api_key: str = ""
    openrouter_model_id: str = "nvidia/nemotron-3-super-120b-a12b:free"
    # Cerebras (optional — narrative-editor non-OR fallback for GATE C).
    # 2026-05-09 — when OR endpoints flake or are throttled, the asides-floor
    # gate has no second-tier path; without Cerebras the gate either retries
    # OR (same upstream) or blocks the email entirely. Cerebras serves
    # llama-3.3-70b at high TPS via OpenAI-compatible API, separate provider.
    # Set CEREBRAS_API_KEY in env to enable; absent key → tier silently
    # skipped (gate falls through to block as before).
    cerebras_api_key: str = ""
    cerebras_model_id: str = "llama-3.3-70b"
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    # Vertex AI (optional — grounded search with Dynamic Retrieval)
    # Set GOOGLE_CLOUD_PROJECT + GOOGLE_APPLICATION_CREDENTIALS_JSON to enable.
    # The tool is silently disabled if google_cloud_project is empty.
    google_cloud_project: str = ""
    google_cloud_region: str = "us-central1"
    google_application_credentials_json: str = ""  # Full JSON content, not a path
    # NIM refine skip flag (JEEVES_SKIP_NIM_REFINE=1 → skip refine even when key is set)
    skip_nim_refine: bool = False
    # Diagnostic dump flag (JEEVES_DEBUG_DRAFTS=1 → write each part's raw +
    # refined draft to sessions/debug-<date>-<label>.html for inspection).
    # Used to triage h3-budget violations and other per-part anomalies.
    debug_drafts: bool = False
    # Groq inter-part sleep in seconds. Must exceed 60s TPM window. JEEVES_GROQ_SLEEP overrides.
    groq_inter_part_sleep_s: int = 65
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

    def briefing_html_path(self, d: date | None = None) -> Path:
        target = d or self.run_date
        suffix = ".local.html" if self.dry_run else ".html"
        return self.repo_root / "sessions" / f"briefing-{target.isoformat()}{suffix}"

    def correspondence_json_path(self, d: date | None = None) -> Path:
        target = d or self.run_date
        suffix = ".local.json" if self.dry_run else ".json"
        return self.repo_root / "sessions" / f"correspondence-{target.isoformat()}{suffix}"

    def correspondence_html_path(self, d: date | None = None) -> Path:
        target = d or self.run_date
        suffix = ".local.html" if self.dry_run else ".html"
        return self.repo_root / "sessions" / f"correspondence-{target.isoformat()}{suffix}"

    @classmethod
    def from_env(
        cls,
        *,
        phase: Phase = "research",
        dry_run: bool = False,
        run_date: date | str | None = None,
        verbose: bool = False,
    ) -> Config:
        load_dotenv()

        resolved_date = _parse_date(run_date)

        required = list(SHARED_REQUIRED)
        if not dry_run:
            required += PHASE_REQUIREMENTS.get(phase, [])

        missing = [n for n in required if not os.environ.get(n)]
        if missing:
            raise MissingSecret(missing)

        return cls(
            nvidia_api_key=os.environ.get("NVIDIA_API_KEY", ""),
            serper_api_key=os.environ.get("SERPER_API_KEY", ""),
            tavily_api_key=os.environ.get("TAVILY_API_KEY", ""),
            exa_api_key=os.environ.get("EXA_API_KEY", ""),
            google_api_key=os.environ.get("GOOGLE_API_KEY", ""),
            groq_api_key=os.environ.get("GROQ_API_KEY", ""),
            gmail_app_password=os.environ.get("GMAIL_APP_PASSWORD", ""),
            gmail_oauth_token_json=os.environ.get("GMAIL_OAUTH_TOKEN_JSON", ""),
            github_token=os.environ.get("GITHUB_TOKEN", ""),
            github_repository=os.environ.get(
                "GITHUB_REPOSITORY", "mmmichaelllang/jeeves-unchained"
            ),
            run_date=resolved_date,
            dry_run=dry_run,
            verbose=verbose,
            phase=phase,
            kimi_model_id=os.environ.get("KIMI_MODEL_ID", "moonshotai/kimi-k2-instruct"),
            kimi_base_url=os.environ.get(
                "KIMI_BASE_URL", "https://integrate.api.nvidia.com/v1"
            ),
            groq_model_id=os.environ.get("GROQ_MODEL_ID", "llama-3.3-70b-versatile"),
            nim_write_model_id=os.environ.get(
                "NIM_WRITE_MODEL_ID", "meta/llama-3.3-70b-instruct"
            ),
            jina_api_key=os.environ.get("JINA_API_KEY", ""),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", ""),
            openrouter_model_id=os.environ.get(
                "OPENROUTER_MODEL_ID", "nvidia/nemotron-3-super-120b-a12b:free"
            ),
            cerebras_api_key=os.environ.get("CEREBRAS_API_KEY", ""),
            cerebras_model_id=os.environ.get("CEREBRAS_MODEL_ID", "llama-3.3-70b"),
            cerebras_base_url=os.environ.get(
                "CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1"
            ),
            google_cloud_project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
            google_cloud_region=os.environ.get("GOOGLE_CLOUD_REGION", "us-central1"),
            google_application_credentials_json=os.environ.get(
                "GOOGLE_APPLICATION_CREDENTIALS_JSON", ""
            ),
            recipient_email=os.environ.get("JEEVES_RECIPIENT_EMAIL", "lang.mc@gmail.com"),
            skip_nim_refine=os.environ.get("JEEVES_SKIP_NIM_REFINE", "").lower()
            in ("1", "true", "yes"),
            debug_drafts=os.environ.get("JEEVES_DEBUG_DRAFTS", "").lower()
            in ("1", "true", "yes"),
            groq_inter_part_sleep_s=int(os.environ.get("JEEVES_GROQ_SLEEP", "65")),
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

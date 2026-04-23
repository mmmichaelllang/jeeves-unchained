"""Terminator tool — the agent calls this once with the final session JSON.

Using a tool-driven terminator (instead of response_format: json_schema) gives
us deterministic behavior across NIM model variants and lets the FunctionAgent
capture the validated payload on a shared context object.
"""

from __future__ import annotations

import logging
from typing import Any

from ..schema import SessionModel, apply_field_caps

log = logging.getLogger(__name__)


class ResearchContext:
    """Shared mutable context passed to the emit_session tool.

    The driver loop in `scripts/research.py` inspects `.session` after the
    agent halts. If the agent stops without filling it we force a fallback.
    """

    def __init__(self):
        self.session: dict[str, Any] | None = None
        self.run_log: list[dict[str, Any]] = []

    @property
    def has_session(self) -> bool:
        return self.session is not None


def make_emit_session(ctx: ResearchContext):
    def emit_session(session_json: dict) -> str:
        """Submit the final, validated session JSON.

        Call exactly once when all sectors have been researched. The payload
        must match the SessionModel schema (see system prompt). After this
        call, stop — the driver will persist and commit the result.

        Args:
            session_json: the full SessionModel-shaped dict.
        """
        if ctx.has_session:
            return "emit_session already called once; ignoring duplicate."

        try:
            apply_field_caps(session_json)
            validated = SessionModel.model_validate(session_json)
        except Exception as e:
            return f"VALIDATION_ERROR: {e}. Fix the payload and try again."

        ctx.session = validated.model_dump(mode="json")
        log.info("emit_session accepted — all sectors captured.")
        return "OK — session accepted. Stop now."

    return emit_session

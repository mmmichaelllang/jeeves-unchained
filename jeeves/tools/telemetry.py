"""Telemetry JSONL — sprint-19 slice E.

Append-only event stream. One line per ``emit()`` call. Default-off.

Activation:
    ``JEEVES_TELEMETRY=1`` env var. Without it, ``emit()`` is a no-op
    (cheap module-level early return — no file I/O, no formatting).

Output:
    ``sessions/telemetry-<utc-date>.jsonl`` at the repo root. Override via
    ``JEEVES_TELEMETRY_DIR=<absolute-path>`` (used in tests + when the
    pipeline runs from an isolated working dir).

Schema:
    Each line is a JSON object with at least::

        {"ts": "<ISO-8601 UTC>", "event": "<event_name>", ...}

    Caller-supplied kwargs are merged in. ``emit`` defends against
    non-JSON-serialisable values by ``repr()``-ing anything that fails
    ``json.dumps``.

Threading:
    Module-level ``threading.Lock`` guards the file handle. Lazy open on
    first write; ``atexit.register`` flushes + closes. Safe to call from
    multiple threads (e.g. the shadow ThreadPoolExecutor).

Why JSONL not structured logging?
    The eval/golden-set harnesses already consume JSONL (see
    ``sessions/shadow-tinyfish-*.jsonl``). One format keeps the analysis
    surface coherent — ``rg event:tool_call`` over a week of runs is
    sufficient to spot regressions.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TextIO

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_FH: Optional[TextIO] = None
_FH_DATE: str = ""
_FH_PATH: Optional[Path] = None


def _enabled() -> bool:
    """Cheap env-var check. Re-evaluated per call so tests can flip it."""
    return os.environ.get("JEEVES_TELEMETRY", "").strip() == "1"


def _output_dir() -> Path:
    override = os.environ.get("JEEVES_TELEMETRY_DIR", "").strip()
    if override:
        return Path(override)
    # Repo-root sessions/ — works when CWD is the repo root (CI default)
    # and when running via ``python -m`` from a checkout.
    return Path.cwd() / "sessions"


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _open_handle() -> Optional[TextIO]:
    """Lazy-open today's telemetry file. Returns None on failure (fail-soft)."""
    global _FH, _FH_DATE, _FH_PATH

    today = _utc_date()
    if _FH is not None and _FH_DATE == today:
        return _FH

    # Date rolled over — close the old handle.
    if _FH is not None:
        try:
            _FH.flush()
            _FH.close()
        except Exception:
            pass
        _FH = None

    out_dir = _output_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"telemetry-{today}.jsonl"
        _FH = path.open("a", encoding="utf-8")
        _FH_DATE = today
        _FH_PATH = path
    except Exception as exc:
        log.debug("telemetry: open failed: %s", exc)
        _FH = None
        _FH_PATH = None
    return _FH


def _safe_jsonable(v: Any) -> Any:
    """Coerce values that fail json.dumps into reprs."""
    try:
        json.dumps(v)
        return v
    except Exception:
        return repr(v)[:500]


def emit(event: str, **fields: Any) -> None:
    """Write one JSONL line if ``JEEVES_TELEMETRY=1``.

    Failure modes are all swallowed — telemetry never breaks the pipeline.
    """
    if not _enabled():
        return
    if not event:
        return

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event": str(event),
    }
    for k, v in fields.items():
        if k in ("ts", "event"):
            continue
        record[k] = _safe_jsonable(v)

    try:
        line = json.dumps(record, ensure_ascii=False)
    except Exception:
        # Last-resort serialiser — shouldn't fire after _safe_jsonable but
        # we guarantee the call site never raises.
        line = json.dumps({"ts": record["ts"], "event": record["event"], "err": "serialize_failed"})

    with _LOCK:
        fh = _open_handle()
        if fh is None:
            return
        try:
            fh.write(line + "\n")
            fh.flush()
        except Exception as exc:
            log.debug("telemetry: write failed: %s", exc)


def current_path() -> Optional[Path]:
    """Return the file path being written to (or None if disabled / not opened).

    Test helper — production code shouldn't depend on this.
    """
    return _FH_PATH


def emit_llm_call(
    *,
    provider: str,
    model: str = "",
    label: str = "",
    sector: str = "",
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    total_tokens: int | None = None,
    latency_ms: float | None = None,
    ok: bool = True,
    error: str = "",
) -> None:
    """Convenience wrapper around ``emit`` for LLM-call accounting.

    Records a ``llm_call`` event per LLM invocation. Token fields are
    optional — providers that surface ``response.usage`` should pass them;
    paths that lack token visibility (Kimi-on-NIM streaming, where usage
    is not always returned) can omit and the rollup will still aggregate
    by call-count and latency.

    Failure mode: same as emit — swallow everything; never break the
    pipeline because of telemetry.
    """
    fields: dict[str, Any] = {
        "provider": provider,
        "ok": bool(ok),
    }
    if model:
        fields["model"] = model
    if label:
        fields["label"] = label
    if sector:
        fields["sector"] = sector
    if prompt_tokens is not None:
        fields["prompt_tokens"] = int(prompt_tokens)
    if completion_tokens is not None:
        fields["completion_tokens"] = int(completion_tokens)
    if total_tokens is not None:
        fields["total_tokens"] = int(total_tokens)
    if latency_ms is not None:
        fields["latency_ms"] = round(float(latency_ms), 1)
    if error:
        fields["error"] = error[:200]
    emit("llm_call", **fields)


def _close() -> None:
    global _FH, _FH_PATH, _FH_DATE
    with _LOCK:
        if _FH is not None:
            try:
                _FH.flush()
                _FH.close()
            except Exception:
                pass
        _FH = None
        _FH_PATH = None
        _FH_DATE = ""


atexit.register(_close)

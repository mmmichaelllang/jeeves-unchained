"""Tests for M7 — Charlotte + Cerebras URL content verification.

All tests are hermetic: no real Charlotte subprocess, no real Cerebras HTTP.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Allow running from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Tests for jeeves/tools/charlotte.py — fetch_url_via_charlotte
# ---------------------------------------------------------------------------


class TestFetchUrlViaCharlotte:

    def test_fetch_url_returns_empty_when_charlotte_not_installed(self):
        """FileNotFoundError from npx → returns '' without raising."""
        from jeeves.tools.charlotte import fetch_url_via_charlotte

        # Patch asyncio.create_subprocess_exec as accessed inside charlotte module.
        with patch(
            "jeeves.tools.charlotte.asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("npx: not found"),
        ):
            result = asyncio.run(fetch_url_via_charlotte("https://example.com"))

        assert result == ""

    def test_fetch_url_returns_empty_on_timeout(self):
        """Subprocess that never responds → returns '' after timeout."""
        from jeeves.tools.charlotte import fetch_url_via_charlotte

        # Create a mock process whose stdout.readline() hangs forever.
        mock_proc = MagicMock()
        mock_proc.returncode = None

        async def _hanging_readline():
            await asyncio.sleep(9999)
            return b""

        mock_stdout = MagicMock()
        mock_stdout.readline = _hanging_readline

        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()

        async def _drain():
            pass

        mock_stdin.drain = _drain
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout

        async def _kill():
            pass

        async def _wait():
            pass

        mock_proc.kill = _kill
        mock_proc.wait = _wait

        async def _fake_exec(*args, **kwargs):
            return mock_proc

        with patch("jeeves.tools.charlotte.asyncio.create_subprocess_exec", new=_fake_exec):
            # Use a very short timeout so the test runs quickly.
            result = asyncio.run(fetch_url_via_charlotte("https://example.com", timeout=0.1))

        assert result == ""


# ---------------------------------------------------------------------------
# Tests for scripts/audit.py — verify_urls_with_charlotte
# ---------------------------------------------------------------------------


_SIMPLE_HTML = """
<html><body>
<p>According to <a href="https://example.com/article">this article</a>, something happened.</p>
<p>See also <a href="https://other.com/page">another page</a>.</p>
</body></html>
"""

_SIMPLE_SESSION: dict = {"date": "2026-05-21", "status": "complete"}


class TestVerifyUrlsWithCharlotte:

    def test_verify_urls_skips_when_flag_off(self):
        """With use_charlotte=False in run_audit, verify_urls_with_charlotte
        is never called and detectors_skipped includes D_charlotte_url_verify."""
        import tempfile

        from scripts.audit import run_audit

        with patch("scripts.audit.verify_urls_with_charlotte") as mock_verify, \
             tempfile.TemporaryDirectory() as tmpdir:
            sessions_dir = Path(tmpdir)

            # Write minimal session + briefing files.
            (sessions_dir / "session-2026-05-21.json").write_text(
                '{"date": "2026-05-21", "status": "complete"}', encoding="utf-8"
            )
            (sessions_dir / "briefing-2026-05-21.html").write_text(
                "<html><body><p>Hello world.</p></body></html>", encoding="utf-8"
            )

            report = run_audit(
                "2026-05-21",
                sessions_dir,
                use_llm=False,
                use_charlotte=False,
            )

        # verify_urls_with_charlotte should NOT have been called.
        mock_verify.assert_not_called()
        # D_charlotte_url_verify should be in skipped list, not run list.
        assert "D_charlotte_url_verify" in report.detectors_skipped
        assert "D_charlotte_url_verify" not in report.detectors_run

    def test_verify_urls_flags_claim_mismatch(self, monkeypatch):
        """When Charlotte returns page text and Cerebras says NO → defect appended."""
        from scripts.audit import verify_urls_with_charlotte, Defect

        # Patch Charlotte fetch to return fake page text.
        async def _fake_fetch(url, timeout=30.0):
            return "This page is about cooking recipes and has nothing to do with the claim."

        # Patch Cerebras call to return NO.
        monkeypatch.setenv("CEREBRAS_API_KEY", "fake-key")

        with patch("scripts.audit.asyncio") as mock_asyncio, \
             patch("scripts.audit._cerebras_verify_claim", return_value="NO"):
            mock_asyncio.run.return_value = "This page is about cooking recipes."

            defects: list[Defect] = []
            verified, flagged = verify_urls_with_charlotte(
                _SIMPLE_HTML, _SIMPLE_SESSION, defects
            )

        assert flagged >= 1
        assert any(d.type == "hallucinated_url" for d in defects)
        matching = [d for d in defects if d.type == "hallucinated_url" and "Charlotte+Cerebras" in d.detail]
        assert len(matching) >= 1
        assert matching[0].severity == "high"
        assert "NO" in matching[0].evidence.get("cerebras_verdict", "")

    def test_verify_urls_skips_on_cerebras_yes(self, monkeypatch):
        """When Cerebras says YES → no defect appended."""
        from scripts.audit import verify_urls_with_charlotte, Defect

        monkeypatch.setenv("CEREBRAS_API_KEY", "fake-key")

        with patch("scripts.audit.asyncio") as mock_asyncio, \
             patch("scripts.audit._cerebras_verify_claim", return_value="YES"):
            mock_asyncio.run.return_value = "This page fully supports the claim."

            defects: list[Defect] = []
            verified, flagged = verify_urls_with_charlotte(
                _SIMPLE_HTML, _SIMPLE_SESSION, defects
            )

        assert flagged == 0
        charlotte_defects = [d for d in defects if "Charlotte+Cerebras" in d.detail]
        assert len(charlotte_defects) == 0

    def test_verify_urls_caps_at_20_urls(self, monkeypatch):
        """With 25 URLs in the html, Charlotte is called at most 20 times."""
        from scripts.audit import verify_urls_with_charlotte, Defect, _CHARLOTTE_URL_CAP

        # Build html with 25 distinct URLs.
        links = "".join(
            f'<a href="https://site{i}.com/page">link {i}</a>\n'
            for i in range(25)
        )
        html = f"<html><body>{links}</body></html>"

        charlotte_call_count = 0

        def _fake_asyncio_run(coro):
            nonlocal charlotte_call_count
            charlotte_call_count += 1
            # Close the coroutine to avoid ResourceWarning.
            coro.close()
            return "page text for claim verification"

        with patch("scripts.audit.asyncio") as mock_asyncio, \
             patch("scripts.audit._cerebras_verify_claim", return_value="YES"):
            mock_asyncio.run.side_effect = _fake_asyncio_run

            defects: list[Defect] = []
            verify_urls_with_charlotte(html, _SIMPLE_SESSION, defects)

        assert charlotte_call_count <= _CHARLOTTE_URL_CAP
        assert charlotte_call_count <= 20

    def test_verify_urls_breaks_on_job_budget(self, monkeypatch):
        """2026-06-17 regression: a slow URL set must not exceed the wall-clock
        budget. After the budget elapses the loop breaks with partial results
        instead of running all 20 URLs (which hung the auditor for ~58 min)."""
        from scripts.audit import verify_urls_with_charlotte, Defect, _CHARLOTTE_JOB_BUDGET_S

        links = "".join(
            f'<a href="https://site{i}.com/page">link {i}</a>\n' for i in range(20)
        )
        html = f"<html><body>{links}</body></html>"

        charlotte_call_count = 0

        def _fake_asyncio_run(coro):
            nonlocal charlotte_call_count
            charlotte_call_count += 1
            coro.close()
            return "page text"

        # Fake clock: t_start=0 (pre-loop), iter1 elapsed=0 (under budget → URL
        # runs), iter2 elapsed=budget+1 (→ break). Net: exactly 1 URL processed.
        clock = iter([0.0, 0.0] + [_CHARLOTTE_JOB_BUDGET_S + 1.0] * 50)

        with patch("scripts.audit.asyncio") as mock_asyncio, \
             patch("scripts.audit._cerebras_verify_claim", return_value="YES"), \
             patch("scripts.audit.time.monotonic", side_effect=lambda: next(clock)):
            mock_asyncio.run.side_effect = _fake_asyncio_run
            defects: list[Defect] = []
            verify_urls_with_charlotte(html, _SIMPLE_SESSION, defects)

        # Only the first URL ran before the budget tripped.
        assert charlotte_call_count == 1, (
            f"expected loop to break after budget, got {charlotte_call_count} calls"
        )

    def test_fetch_url_bounded_wait_does_not_hang(self):
        """The finally-block proc.wait() must be bounded: a subprocess whose
        .wait() never resolves after kill() must NOT hang the caller."""
        from jeeves.tools.charlotte import fetch_url_via_charlotte

        mock_proc = MagicMock()
        mock_proc.returncode = None

        async def _hanging_readline():
            await asyncio.sleep(9999)
            return b""

        mock_stdout = MagicMock()
        mock_stdout.readline = _hanging_readline
        mock_stdin = MagicMock()
        mock_stdin.write = MagicMock()

        async def _drain():
            pass

        mock_stdin.drain = _drain
        mock_proc.stdin = mock_stdin
        mock_proc.stdout = mock_stdout

        async def _kill():
            pass

        async def _never_resolving_wait():
            await asyncio.sleep(9999)

        mock_proc.kill = _kill
        mock_proc.wait = _never_resolving_wait

        async def _fake_exec(*args, **kwargs):
            return mock_proc

        async def _run():
            # Outer guard: if the bounded wait regresses to unbounded, this
            # wait_for fires and the test fails loudly instead of hanging CI.
            return await asyncio.wait_for(
                fetch_url_via_charlotte("https://example.com", timeout=0.1),
                timeout=10.0,
            )

        with patch("jeeves.tools.charlotte.asyncio.create_subprocess_exec", new=_fake_exec):
            result = asyncio.run(_run())

        assert result == ""

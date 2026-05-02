"""Antifragile Playwright + OpenRouter free-model fallback extractor.

Triggered when Jina (talk_of_the_town) or httpx (enrichment) fails to retrieve
clean article content. The extractor uses a thick middleware orchestration
layer that does the mechanical work; the LLM is a high-level navigator only.

Architecture (per the sprint 12 antifragile blueprint):

  1. SANITIZE — heuristic node pruning strips iframes, scripts, styles, nav,
     footer, header, aside, SVG, hidden elements before anything reaches the
     LLM. Free models drown in raw DOM; we never let them see it.

  2. CONSTRAIN — viewport-restricted snapshots; macro action surface
     (``scroll_down``, ``click_text``, ``extract_main``, ``done``, ``give_up``)
     instead of low-level fill/press/click events.

  3. VALIDATE — every LLM output is parsed through a Pydantic schema. On a
     ValidationError the middleware silently re-prompts the model with the
     parse error rather than crashing the loop.

  4. CIRCUIT-BREAK — DOM text is hashed (SHA-256, 16-char prefix). Three
     consecutive commands that produce the same hash trip the breaker;
     middleware resets the page and injects a "Previous action failed to
     mutate page state" warning.

  5. FLUSH — only the original objective, the last 5 actions, and the
     current page state are kept in the prompt. Prior accessibility trees
     are dropped to keep the free model's effective context small.

  6. RECOVER — keyword detection for paywalls/CAPTCHAs/404s automatically
     triggers ``page.go_back()`` and tells the model to pick a different
     search result.

  7. CRYSTALLIZE — once content is acquired, raw HTML is passed to a
     secondary "clean to markdown" prompt that strips remaining web noise
     before handoff to the caller.

Public surface:

  - ``extract_article(url, *, timeout_seconds, max_chars) -> dict`` — the
    happy-path synchronous fetcher. Used by ``talk_of_the_town`` and
    ``enrichment`` as a fallback when their primary fetchers fail.
  - ``run_navigation_session(start_url, objective, *, max_steps) -> dict``
    — the full agent loop with circuit breaker, dead-end recovery, and
    context flushing. Currently exercised only by tests; available for
    future research workflows that need to navigate (search → click →
    extract) rather than just extract a known URL.

Both APIs are fail-soft: missing ``playwright``, missing OpenRouter API key,
network timeout, dead-end detection, or any internal exception → returns
``{'success': False, 'error': '<reason>', 'text': '', ...}``. Callers must
treat this as a fallback path, not a primary fetcher.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy availability checks — playwright and openai are optional.
# ---------------------------------------------------------------------------

_PLAYWRIGHT_AVAILABLE: bool | None = None


def _playwright_available() -> bool:
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is None:
        try:
            import playwright  # noqa: F401
            _PLAYWRIGHT_AVAILABLE = True
        except ImportError:
            _PLAYWRIGHT_AVAILABLE = False
    return _PLAYWRIGHT_AVAILABLE


_OPENAI_AVAILABLE: bool | None = None


def _openai_available() -> bool:
    global _OPENAI_AVAILABLE
    if _OPENAI_AVAILABLE is None:
        try:
            import openai  # noqa: F401
            _OPENAI_AVAILABLE = True
        except ImportError:
            _OPENAI_AVAILABLE = False
    return _OPENAI_AVAILABLE


# ---------------------------------------------------------------------------
# Schema — every LLM response must validate against LLMResponse or be re-prompted.
# ---------------------------------------------------------------------------

# Macro actions exposed to the LLM. Anything outside this enum fails validation.
_VALID_ACTIONS = frozenset({
    "scroll_down",
    "click_text",
    "extract_main",
    "done",
    "give_up",
})


class LLMCommand(BaseModel):
    """Single mechanical command issued by the LLM navigator."""

    action: str = Field(
        ...,
        description=(
            "One of: scroll_down (advance viewport), click_text (semantic "
            "locator), extract_main (return main article body), done "
            "(objective met), give_up (no path forward)."
        ),
    )
    target: str | None = Field(
        None,
        description=(
            "For click_text: the visible text to locate. For extract_main: "
            "optional CSS selector hint (default 'article')."
        ),
    )

    @field_validator("action")
    @classmethod
    def _check_action(cls, v: str) -> str:
        if v not in _VALID_ACTIONS:
            raise ValueError(
                f"action must be one of {sorted(_VALID_ACTIONS)}, got {v!r}"
            )
        return v


class LLMResponse(BaseModel):
    """Strict state-machine response shape. Three-component contract."""

    current_objective: str = Field(..., min_length=1, max_length=300)
    observation: str = Field(..., min_length=1, max_length=400)
    command: LLMCommand


# ---------------------------------------------------------------------------
# Dead-end keyword detection.
# ---------------------------------------------------------------------------

_DEAD_END_KEYWORDS = (
    "access denied",
    "please verify you are human",
    "are you a robot",
    "captcha",
    "rate limit exceeded",
    "403 forbidden",
    "404 not found",
    "page not found",
    "cloudflare ray id",
    "blocked by",
    "subscribe to read",
    "subscribers only",
    "this content is for subscribers",
    "create a free account to continue",
    "verifying you are human",
)


def is_dead_end(text: str) -> bool:
    """Return True if the page text contains a known paywall/CAPTCHA/404 marker."""
    if not text:
        return False
    lower = text.lower()
    return any(k in lower for k in _DEAD_END_KEYWORDS)


# ---------------------------------------------------------------------------
# DOM sanitization.
# ---------------------------------------------------------------------------

# Tags we strip in their entirety (including contents). Hidden / non-content noise.
_NOISE_TAGS = (
    "iframe",
    "svg",
    "noscript",
    "script",
    "style",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
    "button",
    "menu",
    "dialog",
)

# Attributes we drop from any retained tag — kills hidden elements without
# requiring computed-style access.
_HIDDEN_ATTR_RE = re.compile(
    r'\s(aria-hidden|hidden|style|onclick|class|id|data-[\w-]+)\s*=\s*"[^"]*"',
    re.IGNORECASE,
)


def sanitize_html(html: str, *, max_chars: int = 16000) -> str:
    """Strip noise tags, hidden attrs, scripts, styles, nav/footer/header.

    Returns sanitized HTML capped at max_chars. Heuristic but cheap — runs
    before anything is shown to the model and before crystallization.
    """
    if not html:
        return ""

    # Drop entire noise tags (open + close + body).
    for tag in _NOISE_TAGS:
        html = re.sub(
            rf"<{tag}\b[^>]*>.*?</{tag}>",
            " ",
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )
        # Self-closing variants.
        html = re.sub(rf"<{tag}\b[^>]*/?>", " ", html, flags=re.IGNORECASE)

    # Drop comments.
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)

    # Strip noise attributes from retained tags.
    html = _HIDDEN_ATTR_RE.sub("", html)

    # Collapse whitespace.
    html = re.sub(r"\s+", " ", html).strip()

    if len(html) > max_chars:
        html = html[:max_chars]
    return html


# ---------------------------------------------------------------------------
# Markdown crystallization — final cleanup before returning text to caller.
# ---------------------------------------------------------------------------

_BLOCK_TAGS_RE = re.compile(
    r"</(p|div|li|article|section|main|h\d)>",
    re.IGNORECASE,
)
_INLINE_BREAKS_RE = re.compile(r"<(br|hr)\b[^>]*/?>", re.IGNORECASE)


def html_to_markdown(html: str) -> str:
    """Convert sanitized HTML to plain markdown.

    Keeps heading levels, paragraph breaks, link text. Drops every other
    tag. Final-stage transformation; caller has already sanitized.
    """
    if not html:
        return ""

    # Heading prefixes — process before generic tag stripping.
    for level in (6, 5, 4, 3, 2, 1):  # high-to-low so h10 doesn't match h1
        html = re.sub(
            rf"<h{level}\b[^>]*>",
            "\n\n" + ("#" * level) + " ",
            html,
            flags=re.IGNORECASE,
        )

    # Block-level closes → paragraph break.
    html = _BLOCK_TAGS_RE.sub("\n\n", html)
    html = _INLINE_BREAKS_RE.sub("\n", html)

    # Anchor text only; href dropped (we already ran sanitize_html).
    html = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", html, flags=re.DOTALL | re.IGNORECASE)

    # Drop every remaining tag.
    html = re.sub(r"<[^>]+>", "", html)

    # HTML entity decode (basic).
    from html import unescape as _unescape

    html = _unescape(html)

    # Collapse runs of blank lines to a single double-newline.
    html = re.sub(r"\n{3,}", "\n\n", html)
    # Collapse runs of spaces.
    html = re.sub(r"[ \t]+", " ", html)
    return html.strip()


# ---------------------------------------------------------------------------
# Circuit breaker — DOM hash collision detection.
# ---------------------------------------------------------------------------


def dom_hash(text: str) -> str:
    """Return a 16-char SHA-256 prefix of the supplied text."""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


@dataclass
class CircuitBreaker:
    """Trips when the page state stops mutating in response to commands.

    The middleware records the DOM hash after every command. If the most
    recent ``max_repeats`` hashes are identical, the breaker reports tripped.
    The agent loop then forces a page reset and warns the LLM that its
    previous actions had no effect.
    """

    max_repeats: int = 3
    history: list[str] = field(default_factory=list)

    def record(self, h: str) -> bool:
        """Append ``h`` to history and return True if the breaker has tripped."""
        self.history.append(h)
        if len(self.history) < self.max_repeats:
            return False
        last = self.history[-self.max_repeats:]
        return all(x == last[0] for x in last)

    def reset(self) -> None:
        self.history.clear()


# ---------------------------------------------------------------------------
# Context-flushing action log — keep the prompt small.
# ---------------------------------------------------------------------------


@dataclass
class ActionLog:
    """Bounded log of recent commands. The full accessibility tree is NEVER
    retained across cycles — only the textual command summary."""

    max_entries: int = 5
    entries: list[str] = field(default_factory=list)

    def push(self, summary: str) -> None:
        self.entries.append(summary)
        if len(self.entries) > self.max_entries:
            self.entries = self.entries[-self.max_entries:]

    def render(self) -> str:
        if not self.entries:
            return "(no prior actions)"
        return "\n".join(f"- {e}" for e in self.entries)


# ---------------------------------------------------------------------------
# OpenRouter client — free-tier fallback chain.
# ---------------------------------------------------------------------------

# Free-tier models tried in order. The first one available answers the call;
# if it fails the next is tried. Keep this list short — every retry adds
# latency to the fallback path.
_OPENROUTER_FALLBACK_MODELS = (
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-2-9b-it:free",
    "openrouter/auto",
)


def _openrouter_chat(
    system_prompt: str,
    user_prompt: str,
    *,
    api_key: str | None = None,
    max_tokens: int = 512,
    timeout: int = 60,
) -> str:
    """Send a chat completion to OpenRouter's free tier with model fallback.

    Returns the assistant message content, or an empty string on total
    failure. Never raises — callers handle the empty-string case.
    """
    if not _openai_available():
        log.debug("openai SDK not installed; openrouter chat skipped")
        return ""

    api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        log.debug("OPENROUTER_API_KEY missing; openrouter chat skipped")
        return ""

    from openai import OpenAI

    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=timeout,
    )
    last_exc: Exception | None = None
    for model_id in _OPENROUTER_FALLBACK_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.1,  # near-deterministic for navigation
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            last_exc = e
            log.debug("openrouter %s failed: %s", model_id, e)
            continue

    log.warning("All openrouter free-tier models failed: %s", last_exc)
    return ""


# ---------------------------------------------------------------------------
# Strict state-machine system prompt for the navigator.
# ---------------------------------------------------------------------------

_NAV_SYSTEM_PROMPT = """You are a high-level web navigation agent.

A Python middleware drives the browser; you only decide the next macro action.
You must respond with a single JSON object — no prose, no commentary, no
markdown fences. The object MUST contain exactly these three keys:

  - "current_objective": one sentence localizing the immediate goal.
  - "observation":       one sentence summary of the current page state.
  - "command":           an object {"action": <macro>, "target": <optional>}.

Available macros (anything else fails validation):

  - "scroll_down"   — advance the viewport. No target.
  - "click_text"    — click the first visible element matching `target` (text).
  - "extract_main"  — return the main article body. Target = optional CSS hint.
  - "done"          — objective achieved; finish.
  - "give_up"       — no path forward.

Your output MUST parse as a single JSON object. Do NOT prefix with ```json or
any other fence. Do NOT include trailing prose. If you cannot decide, emit
{"current_objective": "...", "observation": "...", "command": {"action": "give_up"}}.
"""


_CRYSTALLIZE_SYSTEM_PROMPT = """You are a content-cleaning specialist.

You receive raw HTML from an article page. Output ONLY the article body as
plain markdown. Drop:
  - navigation, headers, footers, sidebars
  - share buttons, comment widgets, newsletter signups, ad blocks
  - cookie banners, paywall overlays
  - related-stories rails, tag lists, author bio boxes

Preserve:
  - the article title (as a top-level # heading)
  - subheadings (## or ###)
  - paragraph text in reading order
  - block quotes and pull quotes (as > prefixed lines)

Output only the markdown. No commentary. No JSON. No code fences.
"""


# ---------------------------------------------------------------------------
# Public API — the simple article extractor used by TOTT and enrichment.
# ---------------------------------------------------------------------------


def extract_article(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_chars: int = 12000,
    crystallize: bool | None = None,
) -> dict[str, Any]:
    """Open ``url`` headless, extract main article text, return a dict.

    Tries the following selectors in order: ``article``, ``main``,
    ``[role="main"]``, ``body``. The first match longer than 500 chars wins.
    Sanitizes, then optionally crystallizes via OpenRouter (markdown cleanup).

    Returns::

        {
            "url": str,
            "title": str,
            "text": str,             # markdown
            "success": bool,
            "extracted_via": "playwright",
            "error": str (only when success=False),
        }

    Fail-soft on every error path — never raises. Caller must check ``success``.

    ``crystallize`` defaults to True when ``OPENROUTER_API_KEY`` is set, else
    False (skip the LLM cleanup pass).
    """
    base: dict[str, Any] = {
        "url": url,
        "title": "",
        "text": "",
        "success": False,
        "extracted_via": "playwright",
    }

    if not url:
        base["error"] = "empty url"
        return base

    if not _playwright_available():
        log.debug("playwright not installed; skipping fallback for %s", url)
        base["error"] = "playwright not installed"
        return base

    try:
        from playwright.sync_api import (
            TimeoutError as PWTimeoutError,
            sync_playwright,
        )
    except Exception as e:
        log.debug("playwright import failed: %s", e)
        base["error"] = f"playwright import failed: {e}"
        return base

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                )
                page = ctx.new_page()
                try:
                    page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=timeout_seconds * 1000,
                    )
                    # Let JS hydrate; many SPAs need a beat after DOMContentLoaded.
                    page.wait_for_timeout(1500)

                    title = ""
                    try:
                        title = (page.title() or "").strip()
                    except Exception:
                        pass

                    raw_html = ""
                    for selector in ("article", "main", '[role="main"]', "body"):
                        try:
                            el = page.query_selector(selector)
                        except Exception:
                            continue
                        if not el:
                            continue
                        try:
                            html = el.inner_html() or ""
                        except Exception:
                            continue
                        if len(html) > 500:
                            raw_html = html
                            break

                    if not raw_html:
                        base["error"] = "no content selector matched"
                        return base

                    sanitized = sanitize_html(raw_html, max_chars=max_chars * 2)

                    # Dead-end check on the sanitized body, not raw HTML, so
                    # the keyword detection isn't fooled by hidden noise tags.
                    body_text_for_check = re.sub(r"<[^>]+>", " ", sanitized)
                    if is_dead_end(body_text_for_check):
                        base["error"] = "dead-end (paywall/captcha/403/404)"
                        return base

                    # Crystallize via OpenRouter when available, else fall
                    # back to the deterministic regex-based markdown converter.
                    if crystallize is None:
                        crystallize = bool(os.environ.get("OPENROUTER_API_KEY"))

                    text = ""
                    if crystallize:
                        text = _openrouter_chat(
                            _CRYSTALLIZE_SYSTEM_PROMPT,
                            sanitized[:12000],
                            max_tokens=2048,
                            timeout=timeout_seconds,
                        )
                    if not text:
                        text = html_to_markdown(sanitized)

                    if not text or len(text) < 200:
                        base["text"] = text
                        base["title"] = title
                        base["error"] = "extracted text too short"
                        return base

                    base["title"] = title
                    base["text"] = text[:max_chars]
                    base["success"] = True
                    return base
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                    try:
                        ctx.close()
                    except Exception:
                        pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except PWTimeoutError as e:
        log.warning("playwright timeout extracting %s: %s", url, e)
        base["error"] = f"timeout: {e}"
        return base
    except Exception as e:
        log.warning("playwright extraction failed for %s: %s", url, e)
        base["error"] = str(e)
        return base


# ---------------------------------------------------------------------------
# Public API — full antifragile navigation loop. Future-use; tested today.
# ---------------------------------------------------------------------------


_NAV_MAX_PARSE_RETRIES = 2  # silent re-prompts on Pydantic ValidationError


def parse_llm_response(raw: str) -> tuple[LLMResponse | None, str | None]:
    """Parse a raw LLM string into an ``LLMResponse``.

    Returns ``(response, None)`` on success or ``(None, parse_error_message)``
    on failure. The error message is suitable for re-prompting the model.
    Strips markdown fences and prose-around-JSON heuristically before parsing.
    """
    if not raw:
        return None, "empty response"

    candidate = raw.strip()
    # Strip markdown fences if the model ignored the no-fence rule.
    if candidate.startswith("```"):
        candidate = re.sub(r"^```\w*\s*", "", candidate)
        candidate = re.sub(r"\s*```\s*$", "", candidate)
    # Heuristic: pluck the first {...} block.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None, "no JSON object found"
    json_str = candidate[start : end + 1]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return None, f"json decode: {e}"

    try:
        return LLMResponse.model_validate(data), None
    except ValidationError as e:
        # Pydantic's error message is informative — feed it straight back.
        return None, f"schema validation: {e.errors()[:3]}"


def _ask_navigator(
    objective: str,
    pruned_state: str,
    action_log: ActionLog,
    *,
    extra_system_warning: str | None = None,
) -> LLMResponse | None:
    """Single LLM round with parse-retry on schema violation."""
    system = _NAV_SYSTEM_PROMPT
    if extra_system_warning:
        system = system + "\n\nMIDDLEWARE WARNING:\n" + extra_system_warning

    user = (
        f"OBJECTIVE: {objective}\n\n"
        f"RECENT ACTIONS (last {action_log.max_entries}):\n{action_log.render()}\n\n"
        f"CURRENT PAGE STATE (pruned, viewport-only):\n{pruned_state}\n\n"
        "Respond with a single JSON object — current_objective, observation, command."
    )

    last_err: str | None = None
    for _ in range(_NAV_MAX_PARSE_RETRIES + 1):
        if last_err:
            user_with_err = (
                user
                + f"\n\nYOUR PREVIOUS RESPONSE FAILED VALIDATION: {last_err}\n"
                "Re-emit a valid JSON object with the three required keys."
            )
        else:
            user_with_err = user
        raw = _openrouter_chat(system, user_with_err, max_tokens=384)
        if not raw:
            return None
        resp, err = parse_llm_response(raw)
        if resp is not None:
            return resp
        last_err = err

    return None


def run_navigation_session(
    start_url: str,
    objective: str,
    *,
    max_steps: int = 12,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """Drive a Playwright session via the antifragile navigator loop.

    Used when the goal is more than "fetch this URL" — e.g. find an article
    matching `objective` from a search results page. The loop runs at most
    ``max_steps`` cycles. Each cycle:

      1. Snapshot + sanitize page → produce a viewport-restricted pruned state.
      2. Ask the navigator (LLM) for the next macro command.
      3. Validate the response (Pydantic, with parse-retry).
      4. Execute the command via Playwright.
      5. Hash the new DOM; record into the circuit breaker.
      6. If the breaker trips → reset page, warn the model, restart cycle.
      7. Detect dead-ends → ``page.go_back()`` + warning.

    Fail-soft on every error path. Returns the same shape as
    ``extract_article``.
    """
    base: dict[str, Any] = {
        "url": start_url,
        "title": "",
        "text": "",
        "success": False,
        "extracted_via": "playwright_navigator",
        "steps": 0,
    }

    if not _playwright_available():
        base["error"] = "playwright not installed"
        return base
    if not _openai_available() or not os.environ.get("OPENROUTER_API_KEY"):
        base["error"] = "openrouter unavailable"
        return base

    try:
        from playwright.sync_api import (
            TimeoutError as PWTimeoutError,
            sync_playwright,
        )
    except Exception as e:
        base["error"] = f"playwright import failed: {e}"
        return base

    breaker = CircuitBreaker(max_repeats=3)
    action_log = ActionLog(max_entries=5)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(viewport={"width": 1280, "height": 900})
                page = ctx.new_page()
                try:
                    page.goto(
                        start_url,
                        wait_until="domcontentloaded",
                        timeout=timeout_seconds * 1000,
                    )
                    page.wait_for_timeout(1200)

                    warning_for_next: str | None = None
                    for step in range(1, max_steps + 1):
                        base["steps"] = step

                        # Snapshot pruned page state.
                        try:
                            raw_state = page.locator("body").inner_html() or ""
                        except Exception:
                            raw_state = ""
                        pruned = sanitize_html(raw_state, max_chars=4000)
                        body_text = re.sub(r"<[^>]+>", " ", pruned)

                        # Dead-end detection.
                        if is_dead_end(body_text):
                            try:
                                page.go_back(timeout=10000)
                                page.wait_for_timeout(800)
                            except Exception:
                                pass
                            warning_for_next = (
                                "Dead-end detected (paywall/CAPTCHA/404). "
                                "Page state has been reverted via go_back. "
                                "Choose a different path."
                            )
                            action_log.push(f"step {step}: dead-end → go_back")
                            breaker.reset()
                            continue

                        # Circuit breaker check (after dead-end since go_back
                        # mutates state).
                        h = dom_hash(body_text)
                        if breaker.record(h):
                            try:
                                page.reload(timeout=10000)
                                page.wait_for_timeout(800)
                            except Exception:
                                pass
                            warning_for_next = (
                                "Previous 3 actions failed to mutate page state. "
                                "Page reloaded. Re-evaluate target — your last "
                                "approach is not working."
                            )
                            action_log.push(f"step {step}: breaker tripped → reload")
                            breaker.reset()
                            continue

                        # LLM step.
                        resp = _ask_navigator(
                            objective, pruned, action_log,
                            extra_system_warning=warning_for_next,
                        )
                        warning_for_next = None
                        if resp is None:
                            base["error"] = "llm unreachable or invalid"
                            return base

                        action_summary = (
                            f"step {step}: {resp.command.action}"
                            f"{f' :: {resp.command.target}' if resp.command.target else ''}"
                        )
                        action_log.push(action_summary)

                        # Execute the macro.
                        cmd = resp.command
                        if cmd.action == "done":
                            text = html_to_markdown(pruned)
                            base["title"] = page.title() or ""
                            base["text"] = text
                            base["success"] = bool(text and len(text) > 200)
                            base["url"] = page.url
                            if not base["success"]:
                                base["error"] = "navigator said done but text empty"
                            return base
                        if cmd.action == "give_up":
                            base["error"] = (
                                f"navigator gave up: {resp.observation[:200]}"
                            )
                            return base
                        if cmd.action == "scroll_down":
                            try:
                                page.mouse.wheel(0, 800)
                                page.wait_for_timeout(500)
                            except Exception as e:
                                warning_for_next = f"scroll_down failed: {e}"
                            continue
                        if cmd.action == "click_text":
                            target = (cmd.target or "").strip()
                            if not target:
                                warning_for_next = "click_text requires target"
                                continue
                            try:
                                page.get_by_text(target, exact=False).first.click(
                                    timeout=8000
                                )
                                page.wait_for_load_state(
                                    "domcontentloaded", timeout=10000,
                                )
                                page.wait_for_timeout(700)
                            except Exception as e:
                                warning_for_next = (
                                    f"click_text({target!r}) failed: {e}"
                                )
                            continue
                        if cmd.action == "extract_main":
                            sel = (cmd.target or "article").strip()
                            try:
                                el = page.query_selector(sel)
                                if not el:
                                    el = page.query_selector("main")
                                if not el:
                                    el = page.query_selector("body")
                                if not el:
                                    warning_for_next = (
                                        "extract_main: no matching selector"
                                    )
                                    continue
                                inner = el.inner_html() or ""
                            except Exception as e:
                                warning_for_next = f"extract_main failed: {e}"
                                continue
                            sanitized = sanitize_html(inner, max_chars=16000)
                            body_check = re.sub(r"<[^>]+>", " ", sanitized)
                            if is_dead_end(body_check):
                                warning_for_next = (
                                    "extract_main returned a dead-end page; "
                                    "choose another path."
                                )
                                continue

                            crystallized = _openrouter_chat(
                                _CRYSTALLIZE_SYSTEM_PROMPT,
                                sanitized[:12000],
                                max_tokens=2048,
                            )
                            text = crystallized or html_to_markdown(sanitized)
                            if text and len(text) >= 200:
                                base["title"] = page.title() or ""
                                base["text"] = text
                                base["success"] = True
                                base["url"] = page.url
                                return base
                            warning_for_next = (
                                "extract_main produced text shorter than 200 "
                                "chars; try a different selector or scroll."
                            )
                            continue

                    # Loop exhausted without done/give_up.
                    base["error"] = f"max_steps ({max_steps}) exhausted"
                    return base
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                    try:
                        ctx.close()
                    except Exception:
                        pass
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except PWTimeoutError as e:
        base["error"] = f"timeout: {e}"
        return base
    except Exception as e:
        log.warning("navigation session failed for %s: %s", start_url, e)
        base["error"] = str(e)
        return base

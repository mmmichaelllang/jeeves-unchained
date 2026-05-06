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

import atexit
import hashlib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy availability checks — playwright and openai are optional.
# Patchright preferred (Runtime.enable CDP-leak fix, navigator.webdriver
# stripped, --enable-automation removed) — falls back to vanilla Playwright
# transparently.
# ---------------------------------------------------------------------------

_PLAYWRIGHT_AVAILABLE: bool | None = None
_USING_PATCHRIGHT: bool = False


def _playwright_available() -> bool:
    """Return True if either patchright or vanilla playwright is importable.

    Sets module-global _USING_PATCHRIGHT flag for downstream import routing.
    """
    global _PLAYWRIGHT_AVAILABLE, _USING_PATCHRIGHT
    if _PLAYWRIGHT_AVAILABLE is None:
        # Prefer patchright. Same API surface; ships stealth defaults.
        try:
            import patchright  # noqa: F401
            _PLAYWRIGHT_AVAILABLE = True
            _USING_PATCHRIGHT = True
            log.debug("playwright_extractor: using patchright (stealth-patched)")
        except ImportError:
            try:
                import playwright  # noqa: F401
                _PLAYWRIGHT_AVAILABLE = True
                _USING_PATCHRIGHT = False
                log.debug("playwright_extractor: using vanilla playwright")
            except ImportError:
                _PLAYWRIGHT_AVAILABLE = False
    return _PLAYWRIGHT_AVAILABLE


def _import_sync_playwright():
    """Import sync_playwright + TimeoutError from the active backend."""
    if _USING_PATCHRIGHT:
        from patchright.sync_api import (  # type: ignore[import-not-found]
            TimeoutError as PWTimeoutError,
            sync_playwright,
        )
    else:
        from playwright.sync_api import (
            TimeoutError as PWTimeoutError,
            sync_playwright,
        )
    return sync_playwright, PWTimeoutError


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


_TRAFILATURA_AVAILABLE: bool | None = None


def _trafilatura_available() -> bool:
    """Check if trafilatura is importable for high-quality main-content extraction."""
    global _TRAFILATURA_AVAILABLE
    if _TRAFILATURA_AVAILABLE is None:
        try:
            import trafilatura  # noqa: F401
            _TRAFILATURA_AVAILABLE = True
        except ImportError:
            _TRAFILATURA_AVAILABLE = False
    return _TRAFILATURA_AVAILABLE


# ---------------------------------------------------------------------------
# Browser/context singleton — eliminates ~1.5-2s startup cost per fetch.
# Module-level state guarded by a lock for thread safety. atexit cleanup
# ensures chromium is closed even on hard interpreter shutdown.
# ---------------------------------------------------------------------------

_BROWSER_LOCK = threading.Lock()
_PW_INSTANCE: Any = None
_BROWSER: Any = None
_CONTEXT: Any = None
_CONTEXT_NOJS: Any = None  # second context with JS disabled (for static sites)

# Hosts that render server-side and don't need JS — gates the no-JS context.
_NO_JS_HOSTS = frozenset({
    "nytimes.com", "theguardian.com", "ft.com", "apnews.com",
    "reuters.com", "bbc.co.uk", "bbc.com", "arxiv.org", "npr.org",
    "propublica.org", "washingtonpost.com", "wsj.com", "edmondsbeacon.com",
    "myedmondsnews.com", "jacobin.com", "nybooks.com", "lrb.co.uk",
})


def _stealth_launch_args() -> list[str]:
    """Args that improve stealth + reliability + perf in CI environments."""
    return [
        "--no-sandbox",
        "--disable-dev-shm-usage",       # avoids /dev/shm OOM on ubuntu-latest
        "--disable-gpu",
        "--disable-blink-features=AutomationControlled",  # strips webdriver flag
        "--disable-background-networking",
        "--disable-sync",
        "--disable-extensions",
        "--no-first-run",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-breakpad",
        "--disable-component-update",
        "--disable-default-apps",
    ]


# Resource types we drop unconditionally — kills bandwidth + render time.
_BLOCK_RESOURCE_TYPES = frozenset({
    "image", "media", "font", "stylesheet", "imageset", "beacon", "csp_report",
    "ping", "manifest",
})

# Hostname patterns we drop — ad/tracker/analytics noise.
_BLOCK_HOST_RE = re.compile(
    r"(doubleclick|googletagmanager|google-analytics|googlesyndication|"
    r"facebook\.net|hotjar|segment\.io|amplitude|mixpanel|fullstory|"
    r"adsystem|taboola|outbrain|criteo|scorecardresearch|chartbeat|"
    r"quantserve|bing\.com/maps|tealium|piano\.io|tinypass|optimize\.google|"
    r"newrelic\.com|sentry\.io/api|adsrvr\.org|adnxs\.com|krxd\.net)",
    re.IGNORECASE,
)

# Paywall script URLs we always abort — drops metering JS before it counts.
_BLOCK_PAYWALL_RE = re.compile(
    r"(paywall|metered|tinypass|piano\.io|admiral|qualtrics|tp\.media)",
    re.IGNORECASE,
)


def _route_block_handler(route: Any) -> None:
    """Context-level route handler. Aborts heavy/tracker/paywall requests."""
    try:
        req = route.request
        if req.resource_type in _BLOCK_RESOURCE_TYPES:
            return route.abort()
        url = req.url
        if _BLOCK_HOST_RE.search(url) or _BLOCK_PAYWALL_RE.search(url):
            return route.abort()
        return route.continue_()
    except Exception:
        # Route already responded to or aborted — playwright raises if so.
        try:
            return route.continue_()
        except Exception:
            return None


def _make_context(p: Any, browser: Any, *, java_script_enabled: bool = True) -> Any:
    """Create a stealth context with route blocking + init scripts attached."""
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 900},
        java_script_enabled=java_script_enabled,
        ignore_https_errors=True,
        # Note: no custom user_agent — using whatever the patched binary ships.
        # Custom UA pinned to a specific Chrome version is itself a fingerprint.
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
        },
    )
    # Route-level resource blocking — context-wide, applies to every page.
    try:
        ctx.route("**/*", _route_block_handler)
    except Exception as e:
        log.debug("route handler attach failed: %s", e)
    # Init scripts injected into every page before any site JS runs.
    try:
        ctx.add_init_script(_INIT_SCRIPT)
    except Exception as e:
        log.debug("init script attach failed: %s", e)
    return ctx


def _get_shared_context(*, java_script_enabled: bool = True) -> tuple[Any, Any] | None:
    """Return (page, context) using the module-level singleton browser.

    Lazily launches chromium on first call. atexit ensures shutdown.
    Returns None if Playwright unavailable.
    """
    global _PW_INSTANCE, _BROWSER, _CONTEXT, _CONTEXT_NOJS
    if not _playwright_available():
        return None
    with _BROWSER_LOCK:
        if _PW_INSTANCE is None:
            try:
                sync_playwright, _ = _import_sync_playwright()
                _PW_INSTANCE = sync_playwright().start()
                _BROWSER = _PW_INSTANCE.chromium.launch(
                    headless=True,  # CI-only — no display server available
                    args=_stealth_launch_args(),
                )
                atexit.register(_shutdown_browser)
                log.info(
                    "playwright_extractor: launched %s chromium singleton",
                    "patchright" if _USING_PATCHRIGHT else "vanilla",
                )
            except Exception as e:
                log.warning("singleton launch failed: %s", e)
                _PW_INSTANCE = _BROWSER = None
                return None
        if java_script_enabled:
            if _CONTEXT is None:
                try:
                    _CONTEXT = _make_context(_PW_INSTANCE, _BROWSER, java_script_enabled=True)
                except Exception as e:
                    log.warning("context creation failed: %s", e)
                    return None
            ctx = _CONTEXT
        else:
            if _CONTEXT_NOJS is None:
                try:
                    _CONTEXT_NOJS = _make_context(_PW_INSTANCE, _BROWSER, java_script_enabled=False)
                except Exception as e:
                    log.warning("nojs context creation failed: %s", e)
                    return None
            ctx = _CONTEXT_NOJS
    try:
        page = ctx.new_page()
        page.set_default_timeout(8000)
        page.set_default_navigation_timeout(30000)
        return page, ctx
    except Exception as e:
        log.warning("new_page failed: %s", e)
        return None


def _shutdown_browser() -> None:
    """atexit handler — cleanly close the singleton browser + playwright."""
    global _PW_INSTANCE, _BROWSER, _CONTEXT, _CONTEXT_NOJS
    with _BROWSER_LOCK:
        for ctx in (_CONTEXT, _CONTEXT_NOJS):
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:
                    pass
        if _BROWSER is not None:
            try:
                _BROWSER.close()
            except Exception:
                pass
        if _PW_INSTANCE is not None:
            try:
                _PW_INSTANCE.stop()
            except Exception:
                pass
        _PW_INSTANCE = _BROWSER = _CONTEXT = _CONTEXT_NOJS = None


# ---------------------------------------------------------------------------
# Init script: stealth fingerprint patches + JSON-LD extractor + MutationObserver.
# Injected via add_init_script before any site JS runs. Patchright handles most
# of the stealth surface but these patches cover Camoufox-style edge cases too.
# ---------------------------------------------------------------------------

_INIT_SCRIPT = r"""
// Stealth: patches that hide automation indicators. Patchright already does
// most of these at the binary level; these are extra belt-and-suspenders for
// vanilla Playwright fallback.
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  } catch(e) {}
  try {
    if (!('chrome' in window)) {
      window.chrome = {runtime: {}};
    }
  } catch(e) {}
  try {
    const origPlugins = navigator.plugins;
    if (!origPlugins || origPlugins.length === 0) {
      Object.defineProperty(navigator, 'plugins', {
        get: () => [{name:'Chrome PDF Plugin'}, {name:'Chrome PDF Viewer'}, {name:'Native Client'}],
      });
    }
  } catch(e) {}
  try {
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
  } catch(e) {}
  try {
    const origPerm = navigator.permissions && navigator.permissions.query;
    if (origPerm) {
      navigator.permissions.query = (p) => (
        p.name === 'notifications'
          ? Promise.resolve({state: Notification.permission})
          : origPerm.call(navigator.permissions, p)
      );
    }
  } catch(e) {}

  // MutationObserver for "page settled" detection.
  // window.__pwLastMutation is read by _wait_for_settled() after navigation.
  try {
    window.__pwLastMutation = Date.now();
    new MutationObserver(() => { window.__pwLastMutation = Date.now(); })
      .observe(document.documentElement, {
        childList: true, subtree: true, characterData: true, attributes: false
      });
  } catch(e) {}
})();
"""


# JS to extract JSON-LD articleBody — the ground-truth article text emitted
# by most major news sites (NYT, NewYorker, Atlantic, Reuters, BBC, etc.).
# Skip Readability + LLM crystallization entirely when this returns content.
_JSON_LD_EXTRACT_JS = r"""
() => {
  try {
    const scripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of scripts) {
      let raw = (s.textContent || '').trim();
      if (!raw) continue;
      let data;
      try { data = JSON.parse(raw); } catch (e) { continue; }
      const arr = Array.isArray(data) ? data : (data['@graph'] || [data]);
      for (const o of arr) {
        if (!o || typeof o !== 'object') continue;
        const t = o['@type'];
        const isArticle = t && (
          /Article|NewsArticle|BlogPosting|ReportageNewsArticle/.test(
            Array.isArray(t) ? t.join(',') : String(t)
          )
        );
        if (isArticle && o.articleBody && String(o.articleBody).length > 500) {
          let author = '';
          if (o.author) {
            if (Array.isArray(o.author)) {
              author = o.author.map(a => (a && a.name) || '').filter(Boolean).join(', ');
            } else if (typeof o.author === 'object') {
              author = o.author.name || '';
            } else {
              author = String(o.author);
            }
          }
          return {
            articleBody: String(o.articleBody),
            headline: o.headline || '',
            author: author,
            datePublished: o.datePublished || '',
          };
        }
      }
    }
    return null;
  } catch (e) { return null; }
}
"""


# Cookie-banner auto-dismiss — covers the most common GDPR/CMP frameworks
# without requiring the full DuckDuckGo autoconsent bundle (~250KB). Catches
# OneTrust, Cookiebot, Quantcast, Didomi, TrustArc — covers ~80% of cases.
# Multilingual: handles English, French, German, Spanish, Italian.
_AUTOCONSENT_JS = r"""
() => {
  try {
    // Multilingual accept-all button labels.
    const labels = [
      'accept all', 'accept all cookies', 'accept cookies', 'i accept',
      'agree', 'i agree', 'ok', 'got it', 'allow all', 'allow cookies',
      'tout accepter', 'accepter tout', 'accepter', 'autoriser tout',
      'alle akzeptieren', 'akzeptieren', 'einverstanden', 'zustimmen',
      'aceptar todo', 'aceptar', 'aceptar todas',
      'accetta tutto', 'accetto', 'accetta',
    ];
    // Common framework selectors (fast path).
    const selectors = [
      '#onetrust-accept-btn-handler',
      '#CybotCookiebotDialogBodyLevelButtonAccept',
      'button[id*="accept-all" i]',
      'button[class*="accept-all" i]',
      'button[id*="accept" i]:not([id*="reject" i])',
      'button[class*="accept" i]:not([class*="reject" i])',
      '[data-testid*="accept" i]:not([data-testid*="reject" i])',
      'button[aria-label*="accept" i]',
      'a[id*="accept" i]:not([id*="reject" i])',
    ];
    let clicked = false;
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) {
        try { el.click(); clicked = true; break; } catch (e) {}
      }
    }
    if (clicked) return true;
    // Text fallback — scan all clickable elements.
    const candidates = Array.from(document.querySelectorAll('button, a[role="button"], [role="button"]'));
    for (const el of candidates) {
      const text = ((el.textContent || '') + ' ' + (el.value || '')).trim().toLowerCase();
      if (!text || text.length > 40) continue;
      if (labels.some(l => text === l || text === l + '!' || text.startsWith(l))) {
        if (el.offsetParent === null) continue;
        try { el.click(); return true; } catch (e) {}
      }
    }
    return false;
  } catch (e) { return false; }
}
"""


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

    ``trip_count`` persists across resets so the session can give_up after
    too many consecutive trips (see ``run_navigation_session``).
    """

    max_repeats: int = 3
    history: list[str] = field(default_factory=list)
    trip_count: int = 0

    def record(self, h: str) -> bool:
        """Append ``h`` to history and return True if the breaker has tripped."""
        self.history.append(h)
        if len(self.history) < self.max_repeats:
            return False
        last = self.history[-self.max_repeats:]
        tripped = all(x == last[0] for x in last)
        if tripped:
            self.trip_count += 1
        return tripped

    def reset(self) -> None:
        self.history.clear()
        # Note: trip_count is intentionally NOT reset — it persists so the
        # session can give_up after too many trips.


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

# Free-tier models tried in order. Drop gemma-2-9b — known worst paraphrase
# offender. Prefer instruction-following models that follow REPRODUCE VERBATIM
# directives. Cap retries at 2 so a sector full of failed URLs doesn't burn
# the 65min research budget on OpenRouter 429s.
_OPENROUTER_FALLBACK_MODELS = (
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
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


_CRYSTALLIZE_SYSTEM_PROMPT = """You extract — you do NOT write.

INPUT: HTML of a web page (article + boilerplate mixed together).

OUTPUT: A single JSON object, no prose, no fences:
{
  "title": "<exact title text from the page>",
  "byline": "<exact byline or empty string>",
  "article_body_markdown": "<article body, paragraphs separated by blank lines>"
}

ABSOLUTE RULES:
1. REPRODUCE VERBATIM. Every sentence in article_body_markdown must appear
   character-for-character in the input HTML's text nodes. Do not rephrase,
   summarize, condense, expand, modernize, or "fix" anything. Do not add
   transitions. Do not drop sentences you find redundant.
2. If a sentence is not in the input, do NOT emit it. No introductions,
   no conclusions, no "the article discusses…".
3. DROP these blocks entirely (do not summarize them, just omit):
   - navigation, header, footer, sidebar, related-articles rails
   - share buttons, comment widgets, newsletter sign-ups, paywall overlays
   - image captions and photo credits (text inside <figcaption> or directly
     adjacent to an <img>)
   - author bio boxes at the END (the byline at the top stays)
   - cookie banners, GDPR overlays
4. PRESERVE structure: ## / ### for in-body subheads, > for blockquotes and
   pull quotes, - for list items.
5. PRESERVE the lead paragraph even if it begins with a single styled
   capital letter (drop cap). Drop caps belong to the body.
6. If the input contains no recoverable article body (paywall, 404, captcha,
   ad page), emit {"title": "", "byline": "", "article_body_markdown": ""}.

OUTPUT THE JSON OBJECT AND NOTHING ELSE.
"""


class CrystallizeResult(BaseModel):
    """Strict shape for the verbatim-extraction LLM call."""

    title: str = Field(default="")
    byline: str = Field(default="")
    article_body_markdown: str = Field(default="")


def _parse_crystallize(raw: str) -> CrystallizeResult | None:
    """Parse crystallizer output. Returns None on parse failure."""
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```\w*\s*", "", candidate)
        candidate = re.sub(r"\s*```\s*$", "", candidate)
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(candidate[start : end + 1])
        return CrystallizeResult.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as e:
        log.debug("crystallizer parse failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Page-settle detection — replaces hardcoded wait_for_timeout(1500).
# Watches the MutationObserver counter installed by _INIT_SCRIPT.
# ---------------------------------------------------------------------------


def _wait_for_settled(page: Any, *, quiet_ms: int = 800, timeout_ms: int = 6000) -> bool:
    """Wait until the DOM has been quiet for `quiet_ms` ms or `timeout_ms` elapses.

    Returns True if settled, False if timeout reached. Falls back to a fixed
    short sleep if the MutationObserver counter is unavailable (e.g. JS off).
    """
    try:
        page.wait_for_function(
            f"() => (Date.now() - (window.__pwLastMutation || Date.now())) > {quiet_ms}",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        try:
            page.wait_for_timeout(min(quiet_ms, 500))
        except Exception:
            pass
        return False


# ---------------------------------------------------------------------------
# Content-quality scoring — soft-failure detection before declaring success.
# A 600-char "Page not found" boilerplate currently passes len > 500.
# ---------------------------------------------------------------------------


def _score_extraction(text: str, title: str = "") -> tuple[float, str]:
    """Return (score 0-1, reason). Score < 0.6 means treat as soft-failure."""
    if not text:
        return 0.0, "empty"
    if len(text) < 800:
        return 0.2, "too_short"
    sentences = re.split(r"[.!?]+\s", text)
    if len(sentences) < 6:
        return 0.3, "too_few_sentences"
    avg_sentence = sum(map(len, sentences)) / max(len(sentences), 1)
    if avg_sentence < 25:
        return 0.4, "fragmented_likely_nav_links"
    lower = text.lower()
    boilerplate_hits = sum(1 for t in (
        "sign in to read", "subscribe to read", "subscribers only",
        "create a free account", "404 not found", "page not found",
        "javascript is disabled", "please enable javascript",
        "verify you are human", "captcha", "access denied",
    ) if t in lower)
    if boilerplate_hits >= 2:
        return 0.3, "boilerplate_heavy"
    if title and title.lower()[:50] in lower[:500]:
        return 0.95, "ok_title_in_body"
    return 0.85, "ok"


# ---------------------------------------------------------------------------
# Trafilatura wrapper — high-quality main-content extraction in pure Python.
# Already a repo dependency (used by httpx fast path elsewhere).
# ---------------------------------------------------------------------------


def _trafilatura_extract(html: str) -> str:
    """Run trafilatura on raw HTML, return markdown. Empty string on failure."""
    if not _trafilatura_available() or not html:
        return ""
    try:
        import trafilatura
        result = trafilatura.extract(
            html,
            favor_recall=True,
            include_comments=False,
            include_tables=True,
            include_links=False,
            deduplicate=True,
            output_format="markdown",
        )
        return result or ""
    except Exception as e:
        log.debug("trafilatura extract failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Cookie / GDPR consent dismissal.
# ---------------------------------------------------------------------------

# Attribute-based selectors tried first (faster, more specific).
_COOKIE_CONSENT_SELECTORS = (
    "button[id*='accept']",
    "button[class*='accept']",
    "button[data-testid*='accept']",
    "[class*='cookie'] button",
    "[id*='cookie'] button",
    "[id*='consent'] button",
    "[class*='consent'] button",
)

# Visible button-text fallbacks (case-sensitive role locator).
_COOKIE_CONSENT_TEXTS = (
    "Accept All",
    "Accept all",
    "Accept",
    "I Accept",
    "Got it",
    "Agree",
    "OK",
    "Allow all",
)


def _dismiss_cookie_consent(page, *, timeout_ms: int = 1500) -> bool:
    """Dismiss GDPR/cookie-consent banner via in-page JS.

    Uses the multilingual _AUTOCONSENT_JS function — covers OneTrust,
    Cookiebot, Quantcast, Didomi, TrustArc, and ~80% of CMPs across
    English/French/German/Spanish/Italian. ONE evaluate call instead
    of the prior 7-selector × 1500ms loop (~10s when no banner exists).
    Fail-soft: never raises.
    """
    try:
        clicked = page.evaluate(_AUTOCONSENT_JS)
        if clicked:
            log.debug("cookie consent dismissed via autoconsent JS")
            try:
                page.wait_for_timeout(300)
            except Exception:
                pass
            return True
    except Exception as e:
        log.debug("autoconsent JS failed: %s", e)
    return False


# ---------------------------------------------------------------------------
# Public API — the simple article extractor used by TOTT and enrichment.
# ---------------------------------------------------------------------------


def _extract_article_core(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_chars: int = 12000,
    crystallize: bool | None = None,
) -> dict[str, Any]:
    """Open ``url`` headless, extract main article text, return a dict.

    Pipeline (first hit wins):
      1. JSON-LD ``articleBody`` ground truth (zero hallucination risk).
      2. Trafilatura on full hydrated DOM (Python, fast, reliable).
      3. Selector race (``article``/``main``/``[role=main]``/``body``)
         + deterministic html_to_markdown.
      4. LLM crystallizer ONLY if explicitly requested via
         ``crystallize=True`` (default: False — saves OpenRouter quota).

    Browser is a module-level singleton — eliminates ~1.5-2s startup cost
    per fetch. Context-level route blocking drops images/fonts/ads/trackers.
    Cookie banners auto-dismissed via injected multilingual JS.

    Returns::

        {
            "url": str,
            "title": str,
            "text": str,             # markdown
            "success": bool,
            "extracted_via": str,    # "json-ld", "trafilatura", "selector", "llm-crystallize"
            "quality_score": float,  # 0-1 content-quality score
            "error": str (only when success=False),
        }

    Fail-soft on every error path — never raises. Caller must check ``success``.
    """
    base: dict[str, Any] = {
        "url": url,
        "title": "",
        "text": "",
        "success": False,
        "extracted_via": "playwright",
        "quality_score": 0.0,
    }

    if not url:
        base["error"] = "empty url"
        return base

    if not _playwright_available():
        log.debug("playwright not installed; skipping fallback for %s", url)
        base["error"] = "playwright not installed"
        return base

    # crystallize default: OFF. Saves OpenRouter calls — most callers (career
    # sector, enrichment) don't need LLM cleanup once trafilatura runs.
    if crystallize is None:
        crystallize = os.environ.get("JEEVES_PW_USE_LLM_CRYSTALLIZE") == "1"

    try:
        sync_playwright, PWTimeoutError = _import_sync_playwright()
    except Exception as e:
        log.debug("playwright import failed: %s", e)
        base["error"] = f"playwright import failed: {e}"
        return base

    # Decide JS-on vs JS-off context based on host. Static-render hosts skip
    # the JS engine entirely → ~200-500ms saved per fetch.
    js_off_host = any(h in url for h in _NO_JS_HOSTS)
    page_ctx = _get_shared_context(java_script_enabled=not js_off_host)
    if page_ctx is None:
        # Singleton failed — fall through to legacy per-call launch (rare).
        return _extract_article_legacy(url, timeout_seconds=timeout_seconds,
                                       max_chars=max_chars, crystallize=crystallize)
    page, _ctx = page_ctx

    try:
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=timeout_seconds * 1000)
        except PWTimeoutError as e:
            base["error"] = f"goto timeout: {e}"
            return base

        # Wait for DOM to settle (replaces hardcoded 1500ms sleep).
        if not js_off_host:
            _wait_for_settled(page, quiet_ms=600, timeout_ms=4000)
            # Dismiss cookie/GDPR consent banners (single JS call).
            try:
                _dismiss_cookie_consent(page)
            except Exception:
                pass

        title = ""
        try:
            title = (page.title() or "").strip()
        except Exception:
            pass
        base["title"] = title

        # ----- 1. JSON-LD articleBody ground truth -----
        if not js_off_host:
            try:
                jsonld = page.evaluate(_JSON_LD_EXTRACT_JS)
            except Exception:
                jsonld = None
            if jsonld and jsonld.get("articleBody"):
                body_text = str(jsonld["articleBody"]).strip()
                if len(body_text) > 500:
                    score, reason = _score_extraction(body_text, title)
                    base["title"] = jsonld.get("headline", "") or title
                    base["text"] = body_text[:max_chars]
                    base["extracted_via"] = "json-ld"
                    base["quality_score"] = score
                    if score >= 0.6:
                        base["success"] = True
                        return base
                    log.debug("json-ld text scored low (%.2f, %s); trying next strategy", score, reason)

        # ----- 2. Trafilatura on full hydrated DOM -----
        try:
            full_html = page.content() or ""
        except Exception:
            full_html = ""
        if full_html and _trafilatura_available():
            traf_text = _trafilatura_extract(full_html)
            if traf_text and len(traf_text) > 500:
                score, reason = _score_extraction(traf_text, title)
                if score >= 0.6:
                    base["text"] = traf_text[:max_chars]
                    base["extracted_via"] = "trafilatura"
                    base["quality_score"] = score
                    base["success"] = True
                    return base
                log.debug("trafilatura scored low (%.2f, %s); trying next strategy", score, reason)

        # ----- 3. Selector race + deterministic markdown -----
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
            # Use textContent length, not HTML length — script-tag-heavy bodies
            # easily clear 500 chars while having no real content.
            try:
                text_len = len(re.sub(r"<[^>]+>", "", html).strip())
            except Exception:
                text_len = 0
            if text_len > 200:
                raw_html = html
                break

        if not raw_html:
            base["error"] = "no content selector matched"
            return base

        sanitized = sanitize_html(raw_html, max_chars=max_chars * 2)
        body_text_for_check = re.sub(r"<[^>]+>", " ", sanitized)
        if is_dead_end(body_text_for_check):
            base["error"] = "dead-end (paywall/captcha/403/404)"
            return base

        # ----- 4. Optional LLM crystallizer -----
        text = ""
        extraction_method = "selector"
        if crystallize:
            raw_llm = _openrouter_chat(
                _CRYSTALLIZE_SYSTEM_PROMPT,
                sanitized[:12000],
                max_tokens=2048,
                timeout=timeout_seconds,
            )
            parsed = _parse_crystallize(raw_llm)
            if parsed and parsed.article_body_markdown:
                text = parsed.article_body_markdown
                extraction_method = "llm-crystallize"
                if parsed.title and not base["title"]:
                    base["title"] = parsed.title
        if not text:
            text = html_to_markdown(sanitized)
            extraction_method = "selector"

        if not text or len(text) < 200:
            base["text"] = text
            base["error"] = "extracted text too short"
            return base

        score, reason = _score_extraction(text, base["title"])
        base["text"] = text[:max_chars]
        base["extracted_via"] = extraction_method
        base["quality_score"] = score
        if score < 0.5:
            base["error"] = f"low-quality extraction ({reason})"
            return base
        base["success"] = True
        return base

    except PWTimeoutError as e:
        log.warning("playwright timeout extracting %s: %s", url, e)
        base["error"] = f"timeout: {e}"
        return base
    except Exception as e:
        log.warning("playwright extraction failed for %s: %s", url, e)
        base["error"] = str(e)
        return base
    finally:
        try:
            page.close()
        except Exception:
            pass


def _extract_article_legacy(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_chars: int = 12000,
    crystallize: bool = False,
) -> dict[str, Any]:
    """Per-call browser launch fallback used when singleton context is unavailable.

    Should rarely be hit — singleton creation only fails on chromium binary
    issues. Same return shape as extract_article. No JSON-LD or settle helpers
    (those need init scripts only the singleton context attaches).
    """
    base: dict[str, Any] = {
        "url": url, "title": "", "text": "", "success": False,
        "extracted_via": "playwright-legacy", "quality_score": 0.0,
    }
    try:
        sync_playwright, PWTimeoutError = _import_sync_playwright()
    except Exception as e:
        base["error"] = f"playwright import failed: {e}"
        return base
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=_stealth_launch_args())
            try:
                ctx = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    ignore_https_errors=True,
                )
                page = ctx.new_page()
                page.set_default_timeout(8000)
                try:
                    page.goto(url, wait_until="domcontentloaded",
                              timeout=timeout_seconds * 1000)
                    page.wait_for_timeout(800)
                    full_html = page.content() or ""
                    text = _trafilatura_extract(full_html) if _trafilatura_available() else ""
                    if not text:
                        body_html = page.locator("body").inner_html() or ""
                        text = html_to_markdown(sanitize_html(body_html, max_chars=max_chars * 2))
                    title = (page.title() or "").strip()
                    if text and len(text) > 200:
                        score, _ = _score_extraction(text, title)
                        base.update({
                            "title": title, "text": text[:max_chars],
                            "extracted_via": "playwright-legacy",
                            "quality_score": score, "success": score >= 0.5,
                        })
                        if not base["success"]:
                            base["error"] = "low-quality extraction"
                    else:
                        base["error"] = "extracted text too short"
                    return base
                finally:
                    try: page.close()
                    except Exception: pass
                    try: ctx.close()
                    except Exception: pass
            finally:
                try: browser.close()
                except Exception: pass
    except PWTimeoutError as e:
        base["error"] = f"timeout: {e}"
        return base
    except Exception as e:
        base["error"] = str(e)
        return base


# ---------------------------------------------------------------------------
# Public extract_article — wraps _extract_article_core with optional TinyFish
# shadow capture. Sprint-18 rollout (week 1): JEEVES_TINYFISH_SHADOW=1 fires
# TinyFish in a background thread alongside Playwright and appends both
# results to sessions/shadow-tinyfish-<date>.jsonl. Production output is
# always the Playwright result — TinyFish never affects the briefing.
# ---------------------------------------------------------------------------


def _shadow_tinyfish_enabled() -> bool:
    return (
        os.environ.get("JEEVES_TINYFISH_SHADOW", "").strip() == "1"
        and bool(os.environ.get("TINYFISH_API_KEY", "").strip())
    )


def _append_shadow_record(record: dict[str, Any]) -> None:
    """Append a single jsonl line to ``sessions/shadow-tinyfish-<date>.jsonl``.

    Best-effort. Silently swallows any IO error — shadow capture must never
    affect production output.
    """
    try:
        from datetime import datetime, timezone
        from pathlib import Path

        sessions_dir = Path.cwd() / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = sessions_dir / f"shadow-tinyfish-{today}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.debug("shadow capture write failed: %s", exc)


# ---------------------------------------------------------------------------
# Sprint-19: Playwright as a search agent — zero-API-cost SERP scraper.
#
# Uses the no-JS singleton context (~1.2s/call after warm). Return shape
# mirrors serper.make_serper_search so callers can swap providers freely.
# ---------------------------------------------------------------------------


_SEARCH_ENGINES = {
    "ddg": "https://html.duckduckgo.com/html/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
    "brave": "https://search.brave.com/search?q={q}&source=web",
}


def _decode_ddg_link(href: str) -> str:
    """DDG HTML wraps every result in /l/?uddg=<encoded>. Unwrap to the real URL."""
    from urllib.parse import parse_qs, unquote, urlparse

    if not href:
        return href
    try:
        parsed = urlparse(href)
        if parsed.path == "/l/" or "uddg=" in (parsed.query or ""):
            qs = parse_qs(parsed.query or "")
            target = (qs.get("uddg") or [""])[0]
            if target:
                return unquote(target)
    except Exception:
        pass
    return href


def _parse_ddg(page: Any, max_results: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        rows = page.eval_on_selector_all(
            "div.result, div.result__body",
            """rows => rows.slice(0, 30).map(r => {
                const a = r.querySelector('a.result__a, h2 a');
                const s = r.querySelector('.result__snippet, .result-snippet');
                return {
                    title: (a && a.innerText) || '',
                    url: (a && a.getAttribute('href')) || '',
                    snippet: (s && s.innerText) || ''
                };
            })""",
        )
    except Exception as exc:
        log.warning("playwright_search ddg parse error: %s", exc)
        return out
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        u = _decode_ddg_link(str(row.get("url") or ""))
        if not u:
            continue
        out.append(
            {
                "title": str(row.get("title") or "")[:300],
                "url": u,
                "snippet": str(row.get("snippet") or "")[:600],
            }
        )
        if len(out) >= max_results:
            break
    return out


def _parse_bing(page: Any, max_results: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        rows = page.eval_on_selector_all(
            "li.b_algo",
            """rows => rows.slice(0, 30).map(r => {
                const a = r.querySelector('h2 a');
                const s = r.querySelector('.b_caption p, p');
                return {
                    title: (a && a.innerText) || '',
                    url: (a && a.getAttribute('href')) || '',
                    snippet: (s && s.innerText) || ''
                };
            })""",
        )
    except Exception as exc:
        log.warning("playwright_search bing parse error: %s", exc)
        return out
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        u = str(row.get("url") or "")
        if not u:
            continue
        out.append(
            {
                "title": str(row.get("title") or "")[:300],
                "url": u,
                "snippet": str(row.get("snippet") or "")[:600],
            }
        )
        if len(out) >= max_results:
            break
    return out


def _parse_brave(page: Any, max_results: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        rows = page.eval_on_selector_all(
            "div.snippet, div[data-type='web']",
            """rows => rows.slice(0, 30).map(r => {
                const a = r.querySelector('a');
                const t = r.querySelector('.title, .heading-serpresult');
                const s = r.querySelector('.snippet-description, .description, .snippet');
                return {
                    title: (t && t.innerText) || (a && a.innerText) || '',
                    url: (a && a.getAttribute('href')) || '',
                    snippet: (s && s.innerText) || ''
                };
            })""",
        )
    except Exception as exc:
        log.warning("playwright_search brave parse error: %s", exc)
        return out
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        u = str(row.get("url") or "")
        if not u or u.startswith("/"):
            continue
        out.append(
            {
                "title": str(row.get("title") or "")[:300],
                "url": u,
                "snippet": str(row.get("snippet") or "")[:600],
            }
        )
        if len(out) >= max_results:
            break
    return out


_PARSERS = {"ddg": _parse_ddg, "bing": _parse_bing, "brave": _parse_brave}


def search(
    query: str,
    *,
    engine: str = "ddg",
    num: int = 10,
    timeout_seconds: int = 12,
    ledger: Any = None,
) -> dict[str, Any]:
    """Headless SERP scrape — Serper peer at zero API cost.

    Parameters
    ----------
    query:
        Non-empty search string.
    engine:
        One of ``"ddg" | "bing" | "brave"``. DDG default; least bot-detection
        friction on GitHub Actions runners.
    num:
        Max number of organic results (default 10).
    timeout_seconds:
        Per-call wall-clock cap (default 12s; warm singleton typically 1-2s).
    ledger:
        Optional ``QuotaLedger``. Records the call under
        ``"playwright_search"`` so the research-sectors quota guard sees real
        work and the wall-clock cap (``DAILY_HARD_CAPS["playwright_search"]``)
        bites before runaway.

    Returns
    -------
    dict
        ``{provider, query, engine, success, results: [{title, url, snippet,
        provider}], error?}``. Fail-soft: every error path returns
        ``success=False`` with an empty ``results`` list.
    """
    base: dict[str, Any] = {
        "provider": "playwright_search",
        "query": query,
        "engine": engine,
        "success": False,
        "results": [],
    }

    if not (query or "").strip():
        base["error"] = "empty query"
        return base

    if engine not in _SEARCH_ENGINES:
        base["error"] = f"unsupported engine: {engine}"
        return base

    if ledger is not None:
        try:
            from .quota import DAILY_HARD_CAPS, QuotaExceeded  # noqa: F401

            cap = DAILY_HARD_CAPS.get("playwright_search")
            if cap is not None:
                ledger.check_daily_allow("playwright_search", hard_cap=cap)
        except Exception as exc:
            if exc.__class__.__name__ == "QuotaExceeded":
                base["error"] = f"playwright_search daily cap: {exc}"
                return base

    if not _playwright_available():
        base["error"] = "playwright not installed"
        return base

    from urllib.parse import quote_plus

    target_url = _SEARCH_ENGINES[engine].format(q=quote_plus(query))

    # Local imports — module-level imports would create a cycle
    # (rate_limits → telemetry → playwright_extractor) only at first call.
    import time as _time

    from .rate_limits import acquire as _rl_acquire
    from .telemetry import emit as _emit

    page_ctx = _get_shared_context(java_script_enabled=True)
    if page_ctx is None:
        base["error"] = "playwright context unavailable"
        return base
    page, _ctx = page_ctx

    t0 = _time.monotonic()
    try:
        with _rl_acquire("playwright_search"):
            try:
                page.goto(target_url, timeout=timeout_seconds * 1000, wait_until="domcontentloaded")
            except Exception as exc:
                base["error"] = f"navigation error: {exc}"
                _emit(
                    "tool_call",
                    provider="playwright_search",
                    query=query,
                    engine=engine,
                    ok=False,
                    latency_ms=int((_time.monotonic() - t0) * 1000),
                    error=str(exc)[:200],
                )
                return base

            # Settle briefly — DDG/Brave inject results progressively.
            try:
                _wait_for_settled(page, quiet_ms=500, timeout_ms=2500)
            except Exception:
                pass

            parser = _PARSERS[engine]
            results = parser(page, max_results=num)
    finally:
        try:
            page.close()
        except Exception:
            pass

    if ledger is not None:
        try:
            ledger.record("playwright_search", 1)
            ledger.record_daily("playwright_search", 1)
        except Exception:
            pass

    if not results:
        base["error"] = f"playwright_search empty results from {engine}"
        _emit(
            "tool_call",
            provider="playwright_search",
            query=query,
            engine=engine,
            ok=False,
            results=0,
            latency_ms=int((_time.monotonic() - t0) * 1000),
            error="empty results",
        )
        return base

    annotated = [
        {**r, "provider": f"playwright_{engine}"} for r in results
    ]
    base.update({"success": True, "results": annotated})
    _emit(
        "tool_call",
        provider="playwright_search",
        query=query,
        engine=engine,
        ok=True,
        results=len(annotated),
        latency_ms=int((_time.monotonic() - t0) * 1000),
    )
    return base


def extract_article(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_chars: int = 12000,
    crystallize: bool | None = None,
) -> dict[str, Any]:
    """Public wrapper around ``_extract_article_core``.

    Behaviour identical to the core implementation. When
    ``JEEVES_TINYFISH_SHADOW=1`` and ``TINYFISH_API_KEY`` is set, fires
    TinyFish on the same URL in a background thread and writes a comparison
    record to ``sessions/shadow-tinyfish-<date>.jsonl``. The Playwright
    result is always returned; TinyFish output is never substituted.
    """
    if not _shadow_tinyfish_enabled():
        return _extract_article_core(
            url,
            timeout_seconds=timeout_seconds,
            max_chars=max_chars,
            crystallize=crystallize,
        )

    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    def _shadow_call() -> dict[str, Any]:
        from .tinyfish import extract_article as _tf_extract

        t0 = _time.monotonic()
        try:
            res = _tf_extract(url, timeout_seconds=timeout_seconds, max_chars=max_chars)
        except Exception as exc:
            res = {"success": False, "error": f"shadow exception: {exc}", "text": ""}
        res["_latency_ms"] = int((_time.monotonic() - t0) * 1000)
        return res

    pw_t0 = _time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_shadow_call)
        pw_result = _extract_article_core(
            url,
            timeout_seconds=timeout_seconds,
            max_chars=max_chars,
            crystallize=crystallize,
        )
        pw_latency_ms = int((_time.monotonic() - pw_t0) * 1000)
        try:
            tf_result = future.result(timeout=timeout_seconds + 5)
        except Exception as exc:
            tf_result = {"success": False, "error": f"shadow timeout: {exc}", "text": "", "_latency_ms": -1}

    pw_text = pw_result.get("text") or ""
    tf_text = tf_result.get("text") or ""
    record = {
        "url": url,
        "playwright": {
            "success": bool(pw_result.get("success")),
            "char_count": len(pw_text),
            "extracted_via": pw_result.get("extracted_via"),
            "quality_score": pw_result.get("quality_score"),
            "latency_ms": pw_latency_ms,
            "title": (pw_result.get("title") or "")[:200],
            "error": pw_result.get("error"),
            "text_sha16": hashlib.sha256(pw_text.encode("utf-8", "replace")).hexdigest()[:16],
        },
        "tinyfish": {
            "success": bool(tf_result.get("success")),
            "char_count": len(tf_text),
            "quality_score": tf_result.get("quality_score"),
            "latency_ms": tf_result.get("_latency_ms"),
            "title": (tf_result.get("title") or "")[:200],
            "error": tf_result.get("error"),
            "text_sha16": hashlib.sha256(tf_text.encode("utf-8", "replace")).hexdigest()[:16],
        },
    }
    _append_shadow_record(record)
    return pw_result


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
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 900},
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": (
                            "text/html,application/xhtml+xml,application/xml"
                            ";q=0.9,image/webp,*/*;q=0.8"
                        ),
                        "DNT": "1",
                        "Upgrade-Insecure-Requests": "1",
                    },
                    ignore_https_errors=True,
                )
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
                            if breaker.trip_count >= 2:
                                base["error"] = "circuit breaker tripped twice; giving up"
                                return base
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

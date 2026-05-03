---
name: playwright-pro
description: Production-grade Playwright patterns for jeeves-unchained article extraction. Use when touching playwright_extractor.py, the New Yorker fetch path, or enriched_articles fallback. Covers stealth headers, cookie-consent dismissal, paywall detection, and circuit-breaker hardening.
---

# Playwright-Pro — Jeeves

## Stealth Headers
Always launch with realistic headers to avoid bot detection:
```python
ctx = browser.new_context(
    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    viewport={"width": 1280, "height": 900},
    extra_http_headers={
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
    },
    java_script_enabled=True,
    ignore_https_errors=True,
)
```

## Cookie-Consent Dismissal
After page load, try to dismiss GDPR/cookie banners before extraction:
```python
COOKIE_SELECTORS = [
    "button[id*='accept']", "button[class*='accept']",
    "button[data-testid*='accept']", "button[aria-label*='Accept']",
    "[class*='cookie'] button", "[id*='cookie'] button",
    "button:has-text('Accept')", "button:has-text('Accept All')",
    "button:has-text('I Accept')", "button:has-text('Got it')",
    "button:has-text('Agree')", "button:has-text('OK')",
]
def _dismiss_cookies(page, timeout_ms=2000):
    for sel in COOKIE_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=timeout_ms):
                el.click(timeout=timeout_ms)
                page.wait_for_timeout(500)
                return True
        except Exception:
            continue
    return False
```

## Circuit Breaker Enhancement
DOM-hash collision with exponential reset:
- 3 identical hashes → trip (current: correct)
- On trip: reload + warn (current: correct)
- Add: track trip count and give_up after 2 trips in same session

## Paywall Bypass Patterns
- Subscribe/login walls: check for `[href*="subscribe"]` or `[href*="login"]` in page
- Soft paywalls (metered): try scrolling past the overlay first
- Hard paywalls (New Yorker): give_up immediately, use verbatim injection path

## Timeout Hierarchy
- page.goto: timeout_seconds * 1000
- wait_for_timeout hydration: 1500ms (SPAs need this)
- click operations: 8000ms
- load_state waits: 10000ms

"""Charlotte MCP browser wrapper for the jeeves audit pipeline.

Provides ``fetch_url_via_charlotte`` — an async function that spawns the
Charlotte MCP server subprocess, navigates to a URL, and returns the full
page text.  All errors return '' so callers never need to handle exceptions.

Charlotte: https://github.com/ticktockbent/charlotte
Install: npm install -g @ticktockbent/charlotte
Run:     npx @ticktockbent/charlotte --profile core

MCP protocol: JSON-RPC 2.0 over stdin/stdout, newline-delimited messages.
"""

from __future__ import annotations

import asyncio
import json
import logging

log = logging.getLogger("jeeves.tools.charlotte")

# Each message sent to the subprocess must end with a newline.
_INIT_MSG = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "jeeves-audit", "version": "1.0"},
    },
}) + "\n"

_INITIALIZED_NOTIFICATION = json.dumps({
    "jsonrpc": "2.0",
    "method": "notifications/initialized",
    "params": {},
}) + "\n"


def _navigate_msg(url: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "charlotte:navigate",
            "arguments": {"url": url},
        },
    }) + "\n"


_OBSERVE_MSG = json.dumps({
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {
        "name": "charlotte:observe",
        "arguments": {"detail": "full"},
    },
}) + "\n"


async def _read_response(reader: asyncio.StreamReader, msg_id: int) -> dict:
    """Read newline-delimited JSON from reader until we find a response for msg_id.

    Skips notifications (no 'id' key) and responses for other ids.
    Raises asyncio.TimeoutError if the outer timeout fires.
    """
    while True:
        line = await reader.readline()
        if not line:
            return {}
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            log.debug("charlotte: invalid JSON line: %r", line[:120])
            continue
        if obj.get("id") == msg_id:
            return obj
        # Skip notifications / other responses.


def _extract_text_from_response(resp: dict) -> str:
    """Pull plain text out of a tools/call response object.

    Charlotte returns:
      {"result": {"content": [{"type": "text", "text": "..."}]}}
    or error:
      {"error": {"message": "..."}}
    """
    if not resp:
        return ""
    if "error" in resp:
        log.warning("charlotte: tool call error: %s", resp["error"])
        return ""
    result = resp.get("result") or {}
    content = result.get("content") or []
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text") or "")
        elif isinstance(item, str):
            parts.append(item)
    return "\n".join(parts)


async def fetch_url_via_charlotte(url: str, timeout: float = 30.0) -> str:
    """Spawn Charlotte, navigate to url, return full page text.

    Returns '' on any failure — subprocess not found, timeout, JSON parse
    errors, or Charlotte tool errors.  Never raises.
    """
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "npx",
            "@ticktockbent/charlotte",
            "--profile",
            "core",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        log.warning(
            "charlotte: npx not found — Charlotte not installed. "
            "Run: npm install -g @ticktockbent/charlotte"
        )
        return ""
    except Exception as exc:
        log.warning("charlotte: failed to spawn subprocess: %s", exc)
        return ""

    try:
        async with asyncio.timeout(timeout):
            # 1. MCP initialize handshake.
            proc.stdin.write(_INIT_MSG.encode())
            await proc.stdin.drain()
            await _read_response(proc.stdout, 1)

            # 2. Notify initialized (no response expected, but required by spec).
            proc.stdin.write(_INITIALIZED_NOTIFICATION.encode())
            await proc.stdin.drain()

            # 3. Navigate to URL.
            proc.stdin.write(_navigate_msg(url).encode())
            await proc.stdin.drain()
            nav_resp = await _read_response(proc.stdout, 2)
            if not nav_resp or "error" in nav_resp:
                log.warning("charlotte: navigate failed for %s: %s", url, nav_resp)
                return ""

            # 4. Observe (full detail → page text).
            proc.stdin.write(_OBSERVE_MSG.encode())
            await proc.stdin.drain()
            obs_resp = await _read_response(proc.stdout, 3)
            return _extract_text_from_response(obs_resp)

    except TimeoutError:
        log.warning("charlotte: timeout (%.0fs) fetching %s", timeout, url)
        return ""
    except Exception as exc:
        log.warning("charlotte: unexpected error fetching %s: %s", url, exc)
        return ""
    finally:
        try:
            if proc is not None and proc.returncode is None:
                proc.kill()
                # Bound the reap: a wedged Charlotte MCP subprocess (Node +
                # headless browser) can ignore SIGKILL long enough that a bare
                # `await proc.wait()` blocks indefinitely — OUTSIDE the request
                # timeout above. That defeated the per-URL cap and let one URL
                # hang the auditor for ~58 min until the GHA 1h ceiling killed
                # it (2026-06-17 run). Cap the wait; if it expires, leave the
                # orphan for the OS to reap rather than blocking the caller.
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (TimeoutError, asyncio.TimeoutError):
                    log.warning(
                        "charlotte: subprocess did not exit within 5s of kill "
                        "for %s — abandoning to OS reaper", url,
                    )
        except Exception:
            pass

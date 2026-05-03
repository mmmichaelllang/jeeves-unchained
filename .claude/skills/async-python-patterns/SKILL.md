---
name: async-python-patterns
description: Asyncio patterns for jeeves-unchained: converting threading.Thread + time.sleep to asyncio.create_task + asyncio.sleep, using asyncio.to_thread for blocking LLM calls, and structured concurrency with TaskGroup. Use when touching generate_briefing in write.py or any code that mixes sync IO-bound calls with background threads.
---

# Async Python Patterns — Jeeves

## Core Pattern: threading.Thread → asyncio.to_thread + create_task

Before (threading):
```python
t = threading.Thread(target=_refine_bg, args=(label, draft), daemon=True)
t.start()
# ... later ...
t.join(timeout=120)
```

After (asyncio):
```python
task = asyncio.create_task(asyncio.to_thread(_refine_bg_sync, label, draft))
# ... later ...
try:
    await asyncio.wait_for(asyncio.shield(task), timeout=120)
except asyncio.TimeoutError:
    log.warning("NIM refine timed out for [%s]; using raw draft", label)
```

## Core Pattern: time.sleep → asyncio.sleep
```python
# Before
time.sleep(65)
# After
await asyncio.sleep(65)
```

## Entry Point: asyncio.run()
```python
# scripts/write.py (entry point)
import asyncio
briefing_html = asyncio.run(generate_briefing(cfg, session, max_tokens=args.max_tokens))
```

## asyncio.to_thread for sync IO-bound calls
When a sync function does blocking IO (HTTP calls, file reads), wrap it:
```python
result = await asyncio.to_thread(sync_blocking_function, arg1, arg2)
```
This runs the sync function in a thread pool without blocking the event loop.

## _SECTOR_SEMAPHORE equivalent
Use asyncio.Semaphore instead of threading.Semaphore:
```python
_sem = asyncio.Semaphore(1)
async with _sem:
    result = await some_async_task()
```

## Test pattern: wrapping async calls in tests
When tests call an async function directly, wrap with asyncio.run():
```python
# Before (sync)
html = generate_briefing(cfg, session)

# After (async)
import asyncio
html = asyncio.run(generate_briefing(cfg, session))
```

## Test pattern: patching asyncio.sleep in generate_briefing tests
After converting time.sleep → asyncio.sleep, monkeypatch must target the asyncio
module reference inside jeeves.write:
```python
# Before
import time
monkeypatch.setattr(time, "sleep", lambda s: None)

# After — patch asyncio.sleep with a coroutine that does nothing
import asyncio

async def _noop_sleep(s):
    return None

monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
```

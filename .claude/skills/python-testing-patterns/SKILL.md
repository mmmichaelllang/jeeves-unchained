---
name: python-testing-patterns
description: Testing patterns for jeeves-unchained: LLM mock strategies, fixture factories, parametrized integration tests, and async test patterns with pytest. Use when writing tests for research_sectors.py, write.py, or any module that calls NIM/Groq APIs. Covers how to test _parse_sector_output, _json_repair_retry, generate_briefing, and the sector agent chain without hitting real APIs.
---

# Python Testing Patterns — Jeeves

## Key facts
- `asyncio_mode = "auto"` in `pyproject.toml` — no `@pytest.mark.asyncio` needed
- `pytest-asyncio>=1.3.0` is in `[dependency-groups] dev`
- Existing tests live in `tests/test_research_sectors.py` (unit) and `test_research_integration.py` (async chain)
- Run tests: `uv run pytest tests/ -v` (or `python -m pytest` if venv active)

## LLM Mock Strategy
Use `unittest.mock.AsyncMock` for async LLM/agent calls. Patch at the module level
where the symbol is imported, not where it is defined:

```python
from unittest.mock import AsyncMock, MagicMock, patch

async def test_json_repair_retry_valid_response():
    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value='[{"source":"BBC","findings":"x","urls":["https://bbc.com"]}]')

    with patch("jeeves.research_sectors.FunctionAgent", return_value=mock_agent), \
         patch("jeeves.research_sectors.build_kimi_llm", return_value=MagicMock()):
        ...
```

## Fixture Factory Pattern
For `SectorSpec`, use the in-module `SECTOR_SPECS` lookup helper rather than
constructing from scratch — avoids schema drift:

```python
from jeeves.research_sectors import SECTOR_SPECS

def _spec(name: str):
    return next(s for s in SECTOR_SPECS if s.name == name)
```

For custom specs in edge-case tests, construct directly:

```python
from jeeves.research_sectors import SectorSpec

def make_list_spec(name="test_sector"):
    return SectorSpec(name=name, shape="list", instruction="Find stuff.", default=[])
```

## Config Mock
`_json_repair_retry` takes a `Config` object. Use `MagicMock()` — all attribute
accesses return new MagicMocks, which is fine for the LLM construction path when
you patch `build_kimi_llm`:

```python
from unittest.mock import MagicMock
cfg = MagicMock()
cfg.verbose = False
```

## Parametrize for JSON repair cases
```python
@pytest.mark.parametrize("raw,expected_len", [
    ('[{"source":"BBC","findings":"x","urls":["https://a"]}]', 1),   # valid
    ("[{'source':'BBC','findings':'x','urls':['https://a']}]", 1),   # python repr
    ('[{"source":"BBC","findings":"x","urls":["https://a"]},]', 1),  # trailing comma
    ('[{"source":"BBC","findings":"x","urls":["https://a"]},{"source":"XYZ', 1),  # truncated
])
def test_normalize_json_parametrized(raw, expected_len):
    from jeeves.research_sectors import _try_normalize_json
    result = _try_normalize_json(raw, is_array=True)
    assert isinstance(result, list)
    assert len(result) == expected_len
```

## Async tests (`_json_repair_retry`)
`asyncio_mode = "auto"` means just declare the test `async def` — no decorator:

```python
async def test_json_repair_retry_valid_response():
    from jeeves.research_sectors import _json_repair_retry, _ParseFailed, SECTOR_SPECS
    ...
```

## QuotaLedger stub
`_json_repair_retry` accepts `ledger` but only passes it to `build_kimi_llm` (which
is mocked). Use a minimal stub:

```python
class _StubLedger:
    _state = {"providers": {}}
```

## What NOT to duplicate
`tests/test_research_sectors.py` already covers:
- `_try_normalize_json` all 4 repair passes (unit level)
- `_parse_sector_output` string/list/dict/deep shapes, ParseFailed sentinel,
  uncited-item drops, quality filter, truncation repair
- `_python_repr_to_json`, `_remove_trailing_commas`, `_recover_truncated_array`
- Quota snapshot helpers, sector spec coverage

Integration tests should focus on:
- Full `_parse_sector_output -> _ParseFailed -> _json_repair_retry` chain end-to-end
- `_json_repair_retry` mock paths: success, garbage response, empty response
- Parametrized multi-shape coverage across different `SectorSpec.shape` values

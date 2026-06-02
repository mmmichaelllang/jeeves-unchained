"""Item A (m8d): Part 8 literary_pick primary mode, flag-gated default-OFF.

History: vault_insight has never been populated in production sessions
(verified 2026-05-27..2026-06-01). literary_pick IS populated daily by
research but only renders as a UAP-quiet-day Part 7 fallback OR via
Patch F's _maybe_rescue_literary_to_part8 when Part 8 emits empty
placeholder. The Patch F path silently fails when the model emits
non-canonical content in Part 8.

Item A introduces a flag that pins literary_pick as the PRIMARY source
for Part 8 — making Library Stacks render daily without depending on
fragile heuristics. Default-OFF for safe rollout (PR #m8d).
"""
from __future__ import annotations

import os
import pytest

try:
    from jeeves.write import (
        PART_PLAN,
        PART_INSTRUCTIONS_BY_NAME,
        PART8_LITERARY_PICK_INSTRUCTIONS,
        PART8_INSTRUCTIONS,
        _part8_literary_pick_primary_enabled,
        get_part_plan,
        get_part_instructions,
    )
except Exception as exc:
    pytest.skip(f"jeeves.write import failed: {exc}", allow_module_level=True)


class TestFlagCheck:
    """The flag is read at call time (not import time) so tests can
    monkeypatch and CI runners can toggle without restart."""

    def test_flag_default_off(self, monkeypatch):
        monkeypatch.delenv("JEEVES_PART8_LITERARY_PICK_PRIMARY", raising=False)
        assert _part8_literary_pick_primary_enabled() is False

    def test_flag_truthy_values(self, monkeypatch):
        for v in ("1", "true", "True", "TRUE", "yes", "on", "Yes", "ON"):
            monkeypatch.setenv("JEEVES_PART8_LITERARY_PICK_PRIMARY", v)
            assert _part8_literary_pick_primary_enabled() is True, f"failed for {v!r}"

    def test_flag_falsy_values(self, monkeypatch):
        for v in ("", "0", "false", "no", "off", "garbage"):
            monkeypatch.setenv("JEEVES_PART8_LITERARY_PICK_PRIMARY", v)
            assert _part8_literary_pick_primary_enabled() is False, f"failed for {v!r}"


class TestGetPartPlan:
    """get_part_plan() returns PART_PLAN unchanged when flag OFF,
    and an adjusted part8 sector list when flag ON."""

    def test_default_off_returns_original_plan(self, monkeypatch):
        monkeypatch.delenv("JEEVES_PART8_LITERARY_PICK_PRIMARY", raising=False)
        plan = get_part_plan()
        # part8 sectors should match the original
        original_part8 = dict(PART_PLAN)["part8"]
        active_part8 = dict(plan)["part8"]
        assert active_part8 == original_part8
        # All other parts unchanged
        for (l1, s1), (l2, s2) in zip(PART_PLAN, plan):
            assert l1 == l2
            assert s1 == s2

    def test_flag_on_extends_part8_sectors(self, monkeypatch):
        monkeypatch.setenv("JEEVES_PART8_LITERARY_PICK_PRIMARY", "1")
        plan = get_part_plan()
        active_part8 = dict(plan)["part8"]
        assert "literary_pick" in active_part8
        assert "vault_insight" in active_part8

    def test_flag_on_other_parts_unchanged(self, monkeypatch):
        monkeypatch.setenv("JEEVES_PART8_LITERARY_PICK_PRIMARY", "1")
        plan = get_part_plan()
        # Verify part1..part7 and part9 sectors unchanged.
        original = dict(PART_PLAN)
        active = dict(plan)
        for label in ("part1", "part2", "part3", "part4", "part5", "part6", "part7", "part9"):
            assert active[label] == original[label], f"{label} sectors changed unexpectedly"


class TestGetPartInstructions:
    """get_part_instructions(label) returns the flag-aware template."""

    def test_default_off_part8_returns_legacy_template(self, monkeypatch):
        monkeypatch.delenv("JEEVES_PART8_LITERARY_PICK_PRIMARY", raising=False)
        assert get_part_instructions("part8") is PART8_INSTRUCTIONS

    def test_flag_on_part8_returns_literary_pick_template(self, monkeypatch):
        monkeypatch.setenv("JEEVES_PART8_LITERARY_PICK_PRIMARY", "1")
        assert get_part_instructions("part8") is PART8_LITERARY_PICK_INSTRUCTIONS

    def test_other_parts_unaffected_by_flag(self, monkeypatch):
        monkeypatch.setenv("JEEVES_PART8_LITERARY_PICK_PRIMARY", "1")
        for label in ("part1", "part2", "part3", "part4", "part5", "part6", "part7", "part9"):
            assert get_part_instructions(label) is PART_INSTRUCTIONS_BY_NAME[label]


class TestLiteraryPickTemplateContent:
    """The literary-pick template has the expected guard rails."""

    def test_has_literary_pick_available_branch(self):
        t = PART8_LITERARY_PICK_INSTRUCTIONS
        assert "literary_pick.available === true" in t

    def test_has_vault_insight_fallback_branch(self):
        t = PART8_LITERARY_PICK_INSTRUCTIONS
        assert "vault_insight.available === true" in t

    def test_has_neither_available_sentinel(self):
        t = PART8_LITERARY_PICK_INSTRUCTIONS
        assert "library stacks offer nothing fresh this morning" in t

    def test_emits_part8_end_marker(self):
        t = PART8_LITERARY_PICK_INSTRUCTIONS
        assert "<!-- PART8 END -->" in t

    def test_word_target_specified(self):
        """Token-budget protection — prompt must cap word count to keep
        Groq TPM in check."""
        t = PART8_LITERARY_PICK_INSTRUCTIONS
        assert "150-200 words" in t

    def test_url_link_directive_present(self):
        t = PART8_LITERARY_PICK_INSTRUCTIONS
        assert "literary_pick.url" in t


class TestBackwardCompat:
    """Default-OFF means current production behaviour is preserved exactly."""

    def test_legacy_PART_PLAN_unmodified(self):
        """The module-level PART_PLAN constant must NOT have been mutated."""
        part8 = dict(PART_PLAN)["part8"]
        assert part8 == ["vault_insight"]

    def test_legacy_PART_INSTRUCTIONS_BY_NAME_part8_unmodified(self):
        assert PART_INSTRUCTIONS_BY_NAME["part8"] is PART8_INSTRUCTIONS

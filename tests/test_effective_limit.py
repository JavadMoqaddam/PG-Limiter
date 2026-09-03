#!/usr/bin/env python3
"""
Tests for the one invariant the ban decision rests on: a resolved limit is never < 1.

``resolve_effective_limit`` is the single source of truth for how many devices a user
may have, and the enforcement loop bans on ``device_count > limit``. A limit of 0 does
not mean "unlimited" - it makes the very first device a violation, so every affected
user is banned after the usual consecutive scans. Four of its five exits used to return
a stored 0 verbatim, and the widest of them, the general limit, applies to every user
with no special or group limit. Exemption is the whitelist, never a limit of zero.
"""

import pytest

from utils.check_usage import (
    DEFAULT_GENERAL_LIMIT,
    MIN_EFFECTIVE_LIMIT,
    resolve_effective_limit,
)


def config_with(general=2, group_limits=None):
    return {"limits": {"general": general}, "group_limits": group_limits or {}}


class TestUnusableLimitsFallThrough:
    """A limit below 1 must hand over to the next, less specific limit."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -1, -99])
    async def test_special_limit(self, bad):
        got = await resolve_effective_limit(
            username="bob", config=config_with(3), special_limit={"bob": bad}
        )
        assert got == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -1])
    async def test_precomputed_metadata_limit(self, bad):
        got = await resolve_effective_limit(
            username="bob", config=config_with(3), metadata={"effective_ip_limit": bad}
        )
        assert got == 3

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -4])
    async def test_batched_group_limit(self, bad):
        got = await resolve_effective_limit(
            username="bob", config=config_with(3), group_limits={"bob": bad}
        )
        assert got == 3

    @pytest.mark.asyncio
    async def test_group_limit_from_config(self):
        got = await resolve_effective_limit(
            username="bob",
            config=config_with(3, {7: 0}),
            metadata={"group_ids": [7]},
        )
        assert got == 3


class TestGeneralLimit:
    """The general limit has nothing to fall through to, so it is floored instead."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [0, -1, -50])
    async def test_unusable_general_limit_uses_the_documented_default(self, bad):
        got = await resolve_effective_limit(username="bob", config=config_with(bad))
        assert got == DEFAULT_GENERAL_LIMIT

    @pytest.mark.asyncio
    async def test_the_fallback_is_announced(self, caplog):
        """
        Silently correcting it would hide an operator mistake that affects everyone, so
        it is logged at CRITICAL naming the consequence.
        """
        await resolve_effective_limit(username="bob", config=config_with(0))
        assert "ban every active user" in caplog.text

    @pytest.mark.asyncio
    async def test_every_layer_broken_at_once(self):
        got = await resolve_effective_limit(
            username="bob",
            config=config_with(0, {7: 0}),
            metadata={"effective_ip_limit": 0, "group_ids": [7]},
            special_limit={"bob": 0},
            group_limits={"bob": 0},
        )
        assert got == DEFAULT_GENERAL_LIMIT


class TestUsableLimitsAreUntouched:
    """The floor must not change any value that was already a real limit."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("general", [1, 2, 3, 10, 500])
    async def test_general_limit_passes_through(self, general):
        assert await resolve_effective_limit(
            username="bob", config=config_with(general)
        ) == general

    @pytest.mark.asyncio
    async def test_priority_order_is_preserved(self):
        # special > precomputed > batched group > config group > general
        assert await resolve_effective_limit(
            username="bob", config=config_with(9), special_limit={"bob": 1}
        ) == 1
        assert await resolve_effective_limit(
            username="bob", config=config_with(9), metadata={"effective_ip_limit": 5}
        ) == 5
        assert await resolve_effective_limit(
            username="bob", config=config_with(9), group_limits={"bob": 4}
        ) == 4

    @pytest.mark.asyncio
    async def test_the_flat_general_limit_key_is_honoured(self):
        """
        It was unreachable: `config.get("limits", {})` returns `{}` when the key is
        absent, `{}` is a dict, so the nested branch always won and resolved to the
        hardcoded default - ignoring a configured higher limit, the stricter direction.
        """
        assert await resolve_effective_limit(
            username="bob", config={"general_limit": 4}
        ) == 4
        # The nested key still wins when both are present.
        assert await resolve_effective_limit(
            username="bob", config={"limits": {"general": 5}, "general_limit": 9}
        ) == 5


class TestNothingResolvesBelowTheFloor:
    """Sweep the space rather than trusting the enumerated cases above."""

    @pytest.mark.asyncio
    async def test_exhaustive_sweep(self):
        offenders = []
        for special in (None, -1, 0, 1, 3, "abc"):
            for eff in (None, -1, 0, 2, "x"):
                for grp in (None, -1, 0, 4):
                    for general in (-1, 0, 1, 5, "abc", None, "", []):
                        kwargs = {"config": {"limits": {"general": general}}}
                        if special is not None:
                            kwargs["special_limit"] = {"bob": special}
                        if eff is not None:
                            kwargs["metadata"] = {"effective_ip_limit": eff}
                        if grp is not None:
                            kwargs["group_limits"] = {"bob": grp}
                        got = await resolve_effective_limit(username="bob", **kwargs)
                        if got < MIN_EFFECTIVE_LIMIT:
                            offenders.append((kwargs, got))
        assert offenders == [], f"resolved below {MIN_EFFECTIVE_LIMIT}: {offenders[:5]}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("config", [None, {}, {"limits": {}}, {"limits": None}])
    async def test_missing_or_empty_config(self, config):
        got = await resolve_effective_limit(username="bob", config=config)
        assert got >= MIN_EFFECTIVE_LIMIT


class TestConfigLayerRefusesItToo:
    """Defence in depth: the value should not reach the resolver in the first place."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", [0, -3, "0", "-1"])
    async def test_unusable_values_fall_back_to_the_default(self, raw):
        from utils.read_config import normalize_general_limit

        assert normalize_general_limit(raw, "GENERAL_LIMIT", default=2) == 2

    @pytest.mark.asyncio
    async def test_a_database_row_falls_back_to_the_environment_value(self):
        """Not to the built-in - that is the precedence GENERAL_LIMIT already uses."""
        from utils.read_config import normalize_general_limit

        assert normalize_general_limit(0, "general_limit", default=6) == 6
        assert normalize_general_limit("abc", "general_limit", default=6) == 6

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", [1, 2, 5, "7"])
    async def test_usable_values_pass_through(self, raw):
        from utils.read_config import normalize_general_limit

        assert normalize_general_limit(raw, "GENERAL_LIMIT") == int(raw)


class TestBotRefusesItAtInput:
    """The Telegram prompts had no floor, unlike the CLI and the HTTP API."""

    @pytest.mark.parametrize("bad", [0, -1, -99])
    def test_rejected_with_a_pointer_to_the_whitelist(self, bad):
        from telegram_bot.utils import general_limit_rejection

        message = general_limit_rejection(bad)
        assert message is not None
        assert "every user" in message
        assert "whitelist" in message

    @pytest.mark.parametrize("good", [1, 2, 50])
    def test_usable_values_are_accepted(self, good):
        from telegram_bot.utils import general_limit_rejection

        assert general_limit_rejection(good) is None

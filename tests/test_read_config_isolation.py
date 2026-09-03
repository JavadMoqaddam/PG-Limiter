#!/usr/bin/env python3
"""
Regression tests for the configuration cache's isolation guarantee.

``read_config`` keeps one merged ENV+DB dict per process and rebuilds it only when a
setting is written. It used to hand that object out directly, so a caller that edited
what it got - ``config_data["disabled_nodes"].append(id)`` in a Telegram handler, say -
changed what the enforcement loop saw for the rest of the process, with nothing in the
log. Two such bugs were fixed one caller at a time before the same pattern turned up in
eight more places, so the guarantee now lives in ``read_config`` itself: every read is a
deep copy.

These tests pin that down, plus the one deliberate exception: ``read_config_scalar``,
which skips the copy for immutable values and refuses to hand out a container.
"""

import pytest


@pytest.fixture
def db_half():
    """The database half of the configuration, with every nested container non-empty."""
    return {
        "db_config": {
            "general_limit": "4",
            "check_interval": "180",
            "ip_source": "api",
            "group_limits": '{"7": 4, "9": 6}',
            "punishment_steps": '[{"type":"disable","duration":30}]',
            "cdn_nodes": "3,4",
            "cdn_inbounds": "vless-ws,trojan",
            "disabled_nodes": "11,12",
            "group_filter_enabled": "true",
            "group_filter_ids": "7,9",
            "admin_filter_enabled": "true",
            "admin_filter_usernames": "adm1,adm2",
        },
        "special_limits": {"alice": 3, "bob": 5},
        "except_users": ["vip1", "vip2"],
    }


@pytest.fixture
async def config_module(monkeypatch, db_half):
    """utils.read_config with the database half stubbed and the cache left clean."""
    import utils.read_config as read_config_mod

    async def fake_load_db_config():
        # A fresh copy per call, so the stub itself cannot be the thing that shares.
        import copy

        return copy.deepcopy(db_half)

    monkeypatch.setattr(read_config_mod, "load_db_config", fake_load_db_config)
    await read_config_mod.invalidate_config_cache()
    yield read_config_mod
    await read_config_mod.invalidate_config_cache()


class TestReadConfigIsolation:
    """A caller must not be able to reach the process-wide cache."""

    @pytest.mark.asyncio
    async def test_neither_exit_returns_the_cache(self, config_module):
        """Both the fresh-build exit and the cached exit must copy."""
        cold = await config_module.read_config()      # builds and caches
        assert cold is not config_module._config_cache
        warm = await config_module.read_config()      # serves the cache
        assert warm is not config_module._config_cache
        assert cold is not warm

    @pytest.mark.asyncio
    async def test_nested_containers_are_not_shared(self, config_module):
        """A shallow copy would leave these aliased; enforcement reads all of them."""
        first = await config_module.read_config()
        second = await config_module.read_config()

        for key in ("panel", "telegram", "limits", "monitoring", "punishment",
                    "group_filter", "admin_filter", "group_limits", "api",
                    "except_users", "cdn_nodes", "cdn_inbounds", "disabled_nodes"):
            assert first[key] is not second[key], f"{key} is shared between reads"

        assert first["limits"]["special"] is not second["limits"]["special"]
        assert first["punishment"]["steps"] is not second["punishment"]["steps"]
        # Depth 4: the step dicts themselves, which the punishment ladder is built from.
        assert first["punishment"]["steps"][0] is not second["punishment"]["steps"][0]
        assert first["group_filter"]["group_ids"] is not second["group_filter"]["group_ids"]

    @pytest.mark.asyncio
    async def test_editing_a_read_does_not_change_the_next_one(self, config_module):
        """
        The exact mutations the Telegram settings handlers perform.

        Each is followed by a save in production, so the edit itself is meant to be
        local. Before the copy it was not, and a failed save left the phantom value in
        the cache for the life of the process.
        """
        victim = await config_module.read_config()
        victim["disabled_nodes"].remove(11)                     # settings_nodes
        victim["disabled_nodes"].append(99)
        victim["cdn_nodes"].append(98)
        victim["cdn_inbounds"].append("ss-ws")                  # settings_cdn
        del victim["group_limits"][7]                            # group_filter
        victim["group_filter"]["group_ids"].append(555)
        victim["admin_filter"]["admin_usernames"].append("evil")  # admin_filter
        victim["punishment"]["steps"].append({"type": "disable", "duration": 1})
        victim["punishment"]["steps"][0]["duration"] = 999       # punishment
        victim["limits"]["special"]["alice"] = 1
        victim["limits"]["general"] = 1
        victim["except_users"].clear()
        victim["max_warning_count"] = 1
        victim["config_degraded"] = True

        after = await config_module.read_config()
        assert after["disabled_nodes"] == [11, 12]
        assert after["cdn_nodes"] == [3, 4]
        assert after["cdn_inbounds"] == ["vless-ws", "trojan"]
        assert after["group_limits"] == {7: 4, 9: 6}
        assert after["group_filter"]["group_ids"] == [7, 9]
        assert after["admin_filter"]["admin_usernames"] == ["adm1", "adm2"]
        assert len(after["punishment"]["steps"]) == 1
        assert after["punishment"]["steps"][0]["duration"] == 30
        assert after["limits"]["special"] == {"alice": 3, "bob": 5}
        assert after["limits"]["general"] == 4
        assert after["except_users"] == ["vip1", "vip2"]
        assert after["max_warning_count"] == 3
        assert after["config_degraded"] is False

    @pytest.mark.asyncio
    async def test_group_limit_keys_stay_integers(self, config_module):
        """
        A json round-trip would be a cheaper copy but stringifies these keys.

        read_config normalizes them to int on purpose and resolve_effective_limit
        matches a user's numeric group ids against them, so str keys would silently
        drop every group limit.
        """
        config = await config_module.read_config()
        assert config["group_limits"] == {7: 4, 9: 6}
        assert all(isinstance(key, int) for key in config["group_limits"])


class TestReadConfigScalar:
    """The one path that deliberately skips the copy."""

    @pytest.mark.asyncio
    async def test_returns_the_configured_value(self, config_module):
        assert await config_module.read_config_scalar("ip_source", "logs") == "api"
        assert int(await config_module.read_config_scalar("check_interval", 60)) == 180

    @pytest.mark.asyncio
    async def test_missing_key_falls_back_to_the_default(self, config_module):
        assert await config_module.read_config_scalar("not_a_key", "fallback") == "fallback"

    @pytest.mark.asyncio
    async def test_warm_reads_do_not_copy(self, config_module, monkeypatch):
        """
        The point of this function: the SSE loop polls it once per 15s per node, so on
        a 49-node fleet with a 180s interval that is ~588 reads per cycle for one
        string. A deep copy per read would be pure waste.
        """
        await config_module.read_config()          # warm the cache
        copies = []
        real_deepcopy = config_module.copy.deepcopy

        class CountingCopy:
            @staticmethod
            def deepcopy(obj):
                copies.append(1)
                return real_deepcopy(obj)

        monkeypatch.setattr(config_module, "copy", CountingCopy)
        for _ in range(20):
            await config_module.read_config_scalar("ip_source", "logs")
        assert copies == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "key", ["cdn_nodes", "except_users", "limits", "punishment", "group_limits"]
    )
    async def test_containers_are_refused(self, config_module, key):
        """Handing out a list or dict from the cache is the bug the copy prevents."""
        with pytest.raises(TypeError, match="may only be used for immutable"):
            await config_module.read_config_scalar(key)


class TestDegradedConfig:
    """A configuration whose database half failed is served but never cached."""

    @pytest.fixture
    async def broken_db(self, monkeypatch):
        import utils.read_config as read_config_mod

        async def failed_load():
            return {"_load_failed": True}

        monkeypatch.setattr(read_config_mod, "load_db_config", failed_load)
        await read_config_mod.invalidate_config_cache()
        yield read_config_mod
        await read_config_mod.invalidate_config_cache()

    @pytest.mark.asyncio
    async def test_flagged_and_not_cached(self, broken_db):
        first = await broken_db.read_config()
        assert first["config_degraded"] is True
        assert first["except_users"] == []
        # Not cached, so the next call retries instead of freezing the degraded view in.
        assert broken_db._config_cache is None
        assert first is not await broken_db.read_config()

    @pytest.mark.asyncio
    async def test_scalar_reader_still_answers(self, broken_db):
        """
        It must not raise or hang while the database is down: the SSE loop asks for
        ip_source on every pass, and 'logs' is the safe answer when the row that would
        say 'api' cannot be read.
        """
        assert await broken_db.read_config_scalar("ip_source", "logs") == "logs"

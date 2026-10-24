"""Current policy predicates must ignore derived metadata and retain existing rules."""

import pytest


def evaluate(config, facts, username="alice"):
    from utils.user_policy import evaluate_user_policy

    return evaluate_user_policy(username=username, config=config, facts=facts)


def config_with(**settings):
    return {"limits": {"general": 5, "special": {}}, "except_users": [], **settings}


@pytest.mark.parametrize(
    "owner,enabled,names,mode,eligible",
    [
        (None, True, ["alice_admin"], "include", True),
        (None, True, ["alice_admin"], "exclude", True),
        ("other", True, [], "include", True),
        ("other", True, ["alice_admin"], "include", False),
        ("alice_admin", True, ["alice_admin"], "include", True),
        ("alice_admin", True, ["alice_admin"], "exclude", False),
        ("ALICE_ADMIN", True, ["alice_admin"], "include", False),
        ("other", False, ["alice_admin"], "include", True),
        ("other", True, ["alice_admin"], "exclude", True),
    ],
)
def test_existing_admin_predicate(owner, enabled, names, mode, eligible):
    cfg = config_with(admin_filter={"enabled": enabled, "mode": mode, "admin_usernames": names})
    policy = evaluate(cfg, {"group_ids": [], "owner_username": owner, "is_monitored": False})
    assert policy.known and policy.eligible is eligible
    assert policy.limit == 5
    assert policy.reason == ("eligible" if eligible else "admin_filtered")


@pytest.mark.parametrize(
    "groups,enabled,ids,mode,eligible",
    [
        ([7], True, ["7"], "include", True),
        (["7"], True, [7], "include", True),
        ([], True, [], "include", False),
        ([7], True, [], "include", False),
        ([], True, [], "exclude", True),
        ([7], True, [], "exclude", True),
        ([7], True, ["7"], "exclude", False),
        ([7], True, [7], "other", False),
        ([8], True, [7], "other", True),
        ([], False, [7], "include", True),
    ],
)
def test_existing_group_predicate(groups, enabled, ids, mode, eligible):
    policy = evaluate(
        config_with(group_filter={"enabled": enabled, "group_ids": ids, "mode": mode}),
        {"group_ids": groups, "owner_username": None},
    )
    assert policy.known and policy.eligible is eligible
    assert policy.limit == 5
    assert policy.reason == ("eligible" if eligible else "group_filtered")


@pytest.mark.parametrize("username,eligible", [("alice", False), ("Alice", True), ("alice_extra", True)])
def test_whitelist_is_exact_current_configuration(username, eligible):
    policy = evaluate(config_with(except_users=["alice"]), {"is_excepted": False}, username)
    assert policy.known and policy.eligible is eligible
    assert policy.limit == 5
    assert policy.reason == ("eligible" if eligible else "whitelisted")


def test_absent_user_is_unknown_even_when_configuration_whitelists_it():
    policy = evaluate(config_with(except_users=["alice"]), None)
    assert (policy.known, policy.eligible, policy.limit, policy.reason, policy.limit_source) == (
        False,
        False,
        None,
        "unknown",
        None,
    )


def test_stale_metadata_policy_fields_are_ignored():
    policy = evaluate(
        config_with(),
        {
            "group_ids": [],
            "owner_username": None,
            "is_excepted": True,
            "is_monitored": False,
            "special_limit": 9,
            "effective_ip_limit": 9,
        },
    )
    assert (policy.known, policy.eligible, policy.limit, policy.reason, policy.limit_source) == (
        True,
        True,
        5,
        "eligible",
        "general",
    )


@pytest.mark.parametrize(
    "special,groups,want,source",
    [
        (1, {7: 9}, 1, "special"),
        ("3", {"7": 9}, 3, "special"),
        (0, {"7": 3, 8: 7}, 7, "group"),
        (-1, {7: -9, 8: 3}, 3, "group"),
        ("bad", {7: 0, 8: -3}, 5, "general"),
        (None, {7: "bad", 8: "4"}, 4, "group"),
    ],
)
def test_only_usable_current_special_and_maximum_group_limits_have_priority(special, groups, want, source):
    policy = evaluate(
        config_with(limits={"general": 5, "special": {"alice": special}}, group_limits=groups),
        {"group_ids": [7, "8"], "owner_username": None, "effective_ip_limit": 99},
    )
    assert (policy.limit, policy.limit_source) == (want, source)


@pytest.mark.parametrize("general", [None, "bad", 0, -9, []])
def test_bad_general_uses_existing_default(general):
    policy = evaluate(config_with(limits={"general": general, "special": {}}), {})
    assert (policy.limit, policy.limit_source) == (2, "general")


def test_current_limit_is_useful_even_when_user_is_filtered():
    policy = evaluate(
        config_with(
            limits={"general": 5, "special": {"alice": 7}},
            group_filter={"enabled": True, "mode": "include", "group_ids": []},
        ),
        {},
    )
    assert (policy.known, policy.eligible, policy.limit, policy.limit_source) == (True, False, 7, "special")

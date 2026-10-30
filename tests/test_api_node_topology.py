"""Authoritative group/core metadata selects required nodes, including defaults."""

from copy import deepcopy

import pytest

from utils.api_node_scope import build_node_scope
from utils.types import NodeType


def core(cid, tags):
    return {
        "id": cid,
        "type": "xray",
        "config": {"inbounds": [{"tag": t, "protocol": "vless"} for t in tags]},
        "exclude_inbound_tags": [],
        "fallbacks_inbound_tags": [],
    }


@pytest.fixture
def topology():
    return {
        "nodes": [
            NodeType(31, "ordinary", "192.0.2.31", "connected", core_config_id=None),
            NodeType(24, "premium", "192.0.2.24", "connected", core_config_id=2),
        ],
        "groups": [
            {"id": 1, "name": "ordinary", "inbound_tags": ["ordinary"], "is_disabled": False},
            {"id": 2, "name": "premium", "inbound_tags": ["premium"], "is_disabled": False},
        ],
        "cores": [core(1, ["ordinary"]), core(2, ["premium"])],
        "monitored": {24, 31},
    }


@pytest.mark.parametrize("groups, expected", [([1], {31}), ([2], {24}), ([1, 2], {24, 31}), ([], set())])
def test_group_union_uses_the_nodes_effective_core(topology, groups, expected):
    scope = build_node_scope(**topology)
    assert scope.required_nodes({"group_ids": groups}) == expected


def test_deleted_core_assignment_uses_authoritative_default(topology):
    topology["nodes"][1].core_config_id = 99
    scope = build_node_scope(**topology)
    assert scope.required_nodes({"group_ids": [1]}) == {24, 31}
    assert scope.required_nodes({"group_ids": [2]}) == set()


def test_operator_exclusion_and_group_disable_are_distinct_from_missing_metadata(topology):
    topology["monitored"].remove(24)
    topology["groups"][0]["is_disabled"] = True
    scope = build_node_scope(**topology)
    assert scope.required_nodes({"group_ids": [1, 2]}) == set()
    assert scope.required_nodes({"group_ids": [999]}) is None
    assert scope.required_nodes({}) is None


def test_down_node_does_not_hide_healthy_nodes_in_the_same_group(topology):
    topology["cores"][1] = core(2, ["ordinary"])
    topology["nodes"][1].status = "error"
    scope = build_node_scope(**topology)
    assert scope.required_nodes({"group_ids": [1]}) == {31}
    topology["nodes"][0].status = "error"
    scope = build_node_scope(**topology)
    assert scope.required_nodes({"group_ids": [1]}) is None


def test_excluded_and_fallback_parent_tags_do_not_grant_direct_node_access(topology):
    topology["cores"][0] = core(1, ["ordinary", "excluded", "parent"])
    topology["cores"][0]["exclude_inbound_tags"] = ["excluded"]
    topology["cores"][0]["fallbacks_inbound_tags"] = ["parent"]
    topology["groups"][0]["inbound_tags"] = ["excluded", "parent"]
    assert build_node_scope(**topology).required_nodes({"group_ids": [1]}) == set()


def test_synthetic_fallback_child_uses_the_core_own_fallback_relationship(topology):
    topology["cores"][0]["config"]["inbounds"] = [
        {"tag": "parent", "protocol": "vless", "port": 443, "settings": {"fallbacks": [{"dest": 8080}]}},
        {"tag": "child", "protocol": "vless", "port": 8080, "streamSettings": {"network": "tcp"}},
    ]
    topology["cores"][1] = deepcopy(topology["cores"][0])
    topology["cores"][1]["id"] = 2
    topology["cores"][1]["config"]["inbounds"][0]["settings"]["fallbacks"][0]["dest"] = 9999
    topology["groups"][0]["inbound_tags"] = ["child<=>parent"]
    assert build_node_scope(**topology).required_nodes({"group_ids": [1]}) == {31}


def test_managed_wireguard_subnets_and_native_core_select_their_tags(topology):
    topology["cores"][0]["config"]["inbounds"] = [
        {"tag": "ordinary", "protocol": "wireguard", "settings": {"address": ["10.0.0.1/32"]}},
        {"tag": "managed", "protocol": "wireguard", "settings": {"address": ["10.0.1.1/24"]}},
    ]
    topology["cores"][1].update(type="wg", config={"interface_name": "premium"})
    scope = build_node_scope(**topology)
    assert scope.required_nodes({"group_ids": [1]}) == set()
    assert scope.required_nodes({"group_ids": [2]}) == {24}
    topology["groups"][0]["inbound_tags"] = ["managed"]
    assert build_node_scope(**topology).required_nodes({"group_ids": [1]}) == {31}


@pytest.mark.parametrize(
    "fault",
    [
        "missing_default",
        "missing_core_id",
        "duplicate_core",
        "duplicate_group",
        "duplicate_node",
        "unknown_type",
        "missing_inbounds",
        "missing_group_tags",
    ],
)
def test_unknown_topology_is_not_authoritative_empty_scope(topology, fault):
    if fault == "missing_default":
        topology["cores"].pop(0)
    elif fault == "missing_core_id":
        topology["nodes"][0].core_config_id = -1
    elif fault == "duplicate_core":
        topology["cores"].append(topology["cores"][0])
    elif fault == "duplicate_group":
        topology["groups"].append(topology["groups"][0])
    elif fault == "duplicate_node":
        topology["nodes"].append(topology["nodes"][0])
    elif fault == "unknown_type":
        topology["cores"][0]["type"] = "unsupported"
    elif fault == "missing_inbounds":
        topology["cores"][0]["config"] = {}
    else:
        del topology["groups"][0]["inbound_tags"]
    with pytest.raises(ValueError):
        build_node_scope(**topology)

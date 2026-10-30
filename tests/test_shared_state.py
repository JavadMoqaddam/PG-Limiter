"""Snapshots of ACTIVE_USERS must be deep so a report reading one is not mutated by
ongoing ingestion into the live map."""

import time

import utils.shared_state as shared_state
from utils.log_observations import LOG_OBSERVATIONS
from utils.types import ConnectionInfo, DeviceInfo, NodeType, UserType


async def test_snapshot_deep_clones_nested_state(monkeypatch, operational_topics):
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    LOG_OBSERVATIONS.configure({"log_timezone": "UTC", "check_interval": 60, "api_ip_freshness": 60})
    LOG_OBSERVATIONS.register_fleet([NodeType(1, "node", "192.0.2.1", "connected")])
    shared_state.note_node_event(1)
    clock[0] += 60
    shared_state.ACTIVE_USERS.clear()
    device_info = DeviceInfo(
        connections=[ConnectionInfo("1.1.1.1", 1, "node", "Vless", 1059.0)],
        unique_ips={"1.1.1.1"},
        is_multi_device=False,
    )
    shared_state.ACTIVE_USERS["u"] = UserType(
        name="u",
        ip=["1.1.1.1"],
        isp_info={"1.1.1.1": {"isp": "OrigISP"}},
        device_info=device_info,
        group_ids=[1, 2],
    )

    snapshot = await shared_state.get_active_users_snapshot()
    snapped = snapshot["u"]

    # Mutate the live originals after the snapshot is taken.
    shared_state.ACTIVE_USERS["u"].isp_info["1.1.1.1"]["isp"] = "MUTATED"
    shared_state.ACTIVE_USERS["u"].group_ids.append(999)
    device_info.unique_ips.add("9.9.9.9")

    # The snapshot is isolated from those mutations.
    assert snapped.isp_info["1.1.1.1"]["isp"] == "OrigISP"
    assert snapped.group_ids == [1, 2]
    assert snapped.device_info.unique_ips == {"1.1.1.1"}
    # And holds its own objects, not aliases of the live ones.
    assert snapped.isp_info is not shared_state.ACTIVE_USERS["u"].isp_info
    assert snapped.device_info is not device_info
    assert snapped.group_ids is not shared_state.ACTIVE_USERS["u"].group_ids

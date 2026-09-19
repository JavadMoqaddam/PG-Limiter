"""Snapshots of ACTIVE_USERS must be deep so a report reading one is not mutated by
ongoing ingestion into the live map."""

import utils.shared_state as shared_state
from utils.types import DeviceInfo, UserType


async def test_snapshot_deep_clones_nested_state():
    shared_state.ACTIVE_USERS.clear()
    device_info = DeviceInfo(unique_ips={"1.1.1.1"}, is_multi_device=False)
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

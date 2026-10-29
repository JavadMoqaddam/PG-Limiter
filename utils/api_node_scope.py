"""Resolve API evidence scope from PasarGuard groups and effective node cores.

Only topology is retained: never core credentials or user proxy settings. The
same group combination shares a result for the lifetime of a collected scope.
"""

from dataclasses import dataclass, field
from ipaddress import ip_interface

from utils.types import PanelType


def _ids(value) -> frozenset[int]:
    if not isinstance(value, list) or any(type(v) is not int or v < 1 for v in value):
        raise ValueError("User group membership is unavailable")
    return frozenset(value)


def _tags(value) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise ValueError("Inbound topology is unavailable")
    return frozenset(value)


def _core_tags(core: dict) -> frozenset[str]:
    """Match the managed inbound selection of PasarGuard 5.5.1 core classes."""
    config = core.get("config")
    if not isinstance(config, dict):
        raise ValueError("Core configuration is unavailable")
    excluded = _tags(core.get("exclude_inbound_tags"))
    fallback_tags = _tags(core.get("fallbacks_inbound_tags"))
    core_type = core.get("type") or "xray"
    if core_type == "wg":
        if excluded or fallback_tags:
            raise ValueError("Unsupported WireGuard inbound exclusions")
        return _tags([config.get("interface_name")])
    if core_type != "xray":
        raise ValueError("Unsupported core type")
    inbounds = config.get("inbounds")
    if not isinstance(inbounds, list) or not inbounds:
        raise ValueError("Core inbounds are unavailable")
    tags = set()
    parents = []
    seen = set()
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            raise ValueError("Malformed core inbound")
        tag = inbound.get("tag")
        _tags([tag])
        if tag in seen:
            raise ValueError("Duplicate core inbound")
        seen.add(tag)
        settings = inbound.get("settings") or {}
        if not isinstance(settings, dict):
            raise ValueError("Malformed inbound settings")
        fallbacks = settings.get("fallbacks") or []
        if not isinstance(fallbacks, list) or any(not isinstance(f, dict) for f in fallbacks):
            raise ValueError("Malformed inbound fallbacks")
        if tag in fallback_tags or fallbacks:
            parents.append(inbound)
    for inbound in inbounds:
        tag, protocol = inbound["tag"], inbound.get("protocol")
        if not isinstance(protocol, str):
            raise ValueError("Missing inbound protocol")
        if tag in excluded | fallback_tags:
            continue
        if protocol not in {"vmess", "vless", "trojan", "shadowsocks", "hysteria", "masque", "wireguard"}:
            continue
        settings = inbound.get("settings") or {}
        if protocol == "wireguard":
            addresses = settings.get("address") or []
            if not isinstance(addresses, list) or any(not isinstance(a, str) for a in addresses):
                raise ValueError("Malformed WireGuard address list")
            if any("/" in a and ip_interface(a).network.num_addresses >= 4 for a in addresses):
                tags.add(tag)
            continue
        tags.add(tag)
        # The panel creates synthetic child<=>parent tags for fallback listeners.
        if inbound.get("streamSettings") and not settings.get("fallbacks"):
            for parent in parents:
                if any(
                    f.get("dest", "") == inbound.get("listen") or f.get("dest", "") == inbound.get("port", 0)
                    for f in (parent.get("settings") or {}).get("fallbacks", [])
                ):
                    tags.add(f"{tag}<=>{parent['tag']}")
    return frozenset(tags)


@dataclass
class APINodeScope:
    """One validated topology snapshot, with a bounded per-cycle lookup cache."""

    group_tags: dict[int, frozenset[str]]
    node_tags: dict[int, frozenset[str]]
    available_nodes: frozenset[int]
    _combinations: dict[frozenset[int], frozenset[int]] = field(default_factory=dict, compare=False, repr=False)

    @property
    def monitored_nodes(self) -> frozenset[int]:
        return frozenset(self.node_tags)

    def required_nodes(self, raw: dict) -> frozenset[int] | None:
        """None means unknown membership; an authoritative empty set is distinct."""
        try:
            groups = _ids(raw.get("group_ids"))
        except ValueError:
            return None
        if not groups.issubset(self.group_tags):
            return None
        if groups not in self._combinations:
            tags = frozenset().union(*(self.group_tags[g] for g in groups))
            self._combinations[groups] = frozenset(
                n for n, inbound_tags in self.node_tags.items() if tags & inbound_tags
            )
        applicable = self._combinations[groups]
        required = applicable & self.available_nodes
        # No healthy applicable node cannot establish that an existing warning
        # normalized. Other users/groups can still supply usable evidence.
        return None if applicable and not required else required


def build_node_scope(nodes, groups: list[dict], cores: list[dict], monitored: set[int]) -> APINodeScope:
    """Build shared group/core indexes without trusting the IP response's keys."""
    group_tags = {}
    for group in groups:
        gid = group.get("id")
        if type(gid) is not int or gid < 1 or gid in group_tags or type(group.get("is_disabled")) is not bool:
            raise ValueError("Malformed group topology")
        if "inbound_tags" not in group:
            raise ValueError("Group inbound topology is unavailable")
        tags = _tags([] if group["inbound_tags"] is None else group["inbound_tags"])
        group_tags[gid] = frozenset() if group["is_disabled"] else tags
    core_rows = {}
    for core in cores:
        cid = core.get("id")
        if type(cid) is not int or cid < 1 or cid in core_rows:
            raise ValueError("Malformed core topology")
        core_rows[cid] = core
    core_tags = {}
    node_tags = {}
    available = set()
    for node in nodes:
        if type(node.node_id) is not int or node.node_id in node_tags:
            raise ValueError("Malformed node topology")
        if node.node_id not in monitored:
            continue
        cid = node.core_config_id
        if cid is None:
            cid = 1
        if type(cid) is not int or cid < 1:
            raise ValueError("Node core configuration is unavailable")
        # PasarGuard resolves null/deleted core assignments through default core 1.
        cid = cid if cid in core_rows else 1
        if cid not in core_rows:
            raise ValueError("Default core configuration is unavailable")
        if cid not in core_tags:
            core_tags[cid] = _core_tags(core_rows[cid])
        node_tags[node.node_id] = core_tags[cid]
        if node.status == "connected":
            available.add(node.node_id)
    if set(node_tags) != monitored:
        raise ValueError("Monitored node topology is incomplete")
    return APINodeScope(group_tags, node_tags, frozenset(available))


async def load_node_scope(panel: PanelType, monitored: set[int]) -> APINodeScope:
    """Fetch shared metadata per cycle; permission/error responses remain unknown."""
    from utils.panel_api.nodes import get_nodes
    from utils.panel_api.request_helper import panel_get

    async def rows(path, key, total):
        response = await panel_get(panel, path)
        if response is None or response.status_code != 200:
            raise ValueError(f"API node scope unavailable: {path}")
        try:
            body = response.json()
            result = body[key]
            if (
                not isinstance(result, list)
                or any(not isinstance(row, dict) for row in result)
                or type(body[total]) is not int
                or body[total] != len(result)
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Incomplete API node scope: {path}") from None
        return result

    groups = await rows("/api/groups", "groups", "total")
    cores = await rows("/api/cores", "cores", "count")
    # _resolve_node_context freshly populated this cache before this call.
    nodes = await get_nodes(panel)
    return build_node_scope(nodes, groups, cores, monitored)

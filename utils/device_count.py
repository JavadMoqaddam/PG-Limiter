"""
Device counting: how many distinct devices a user's connections represent.

Every enforcement decision is made on this number - the warning path, the ban
path and the "user normalized" clearing path all compare it against the user's
limit - so it lives in its own module with no database, panel or Telegram
imports, and is unit tested directly.

Two counting modes exist because operators deploy nodes differently. Neither
mode ever puts ``node_id`` in the key: several nodes commonly serve one core
config, so a single client is registered on all of them at once and would
otherwise be counted once per node.

    "device"  (ip-or-subnet, inbound)  one IP reaching two inbounds counts as
                                       two devices. This is what catches
                                       several people sharing one connection.
    "ip"      (ip-or-subnet)           one client IP is exactly one device.

CDN traffic is collapsed before either rule applies: a whole CDN node, or a
whole CDN inbound, is one device no matter how many addresses it presents.
"""

import ipaddress
from dataclasses import dataclass, field

from utils.types import ConnectionInfo, EnhancedUserInfo, UserType

# Counting modes, as stored in the ``device_count_mode`` config key.
COUNT_MODE_DEVICE = "device"
COUNT_MODE_IP = "ip"


@dataclass(slots=True)
class DeviceCountingConfig:
    """Configuration container for device counting and IP grouping rules."""

    cdn_inbounds: list[str] = field(default_factory=list)
    cdn_nodes: list[int] = field(default_factory=list)
    disabled_nodes: list[int] = field(default_factory=list)
    subnet_ip_grouping: bool = False
    subnet_grouping_mode: str = "/24"  # "/24" (standard) or "/16" (wide + ISP)
    high_trust_ip_grouping: bool = False
    high_trust_threshold: int = 20
    count_mode: str = COUNT_MODE_DEVICE

    @classmethod
    def from_config(cls, config_data: dict) -> "DeviceCountingConfig":
        """Build DeviceCountingConfig from application config dictionary."""
        return cls(
            cdn_inbounds=config_data.get("cdn_inbounds", []) or [],
            cdn_nodes=config_data.get("cdn_nodes", []) or [],
            disabled_nodes=config_data.get("disabled_nodes", []) or [],
            subnet_ip_grouping=config_data.get("subnet_ip_grouping", False),
            subnet_grouping_mode=config_data.get("subnet_grouping_mode", "/24"),
            high_trust_ip_grouping=config_data.get("high_trust_ip_grouping", False),
            high_trust_threshold=config_data.get("high_trust_threshold", 20),
            count_mode=config_data.get("device_count_mode", COUNT_MODE_DEVICE),
        )


def subnet_key(ip: str) -> str:
    """Return the /24 subnet of an IPv4 address; anything else is returned as is."""
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if ip_obj.version != 4:
        return ip  # IPv6: the full address is the key
    network = ipaddress.ip_network(f"{ip}/24", strict=False)
    return network.network_address.exploded.rsplit(".", 1)[0]


def wide_subnet_key(ip: str) -> str:
    """Return the /16 subnet (first two octets) of an IPv4 address."""
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if ip_obj.version != 4:
        return ip  # IPv6: the full address is the key
    network = ipaddress.ip_network(f"{ip}/16", strict=False)
    parts = network.network_address.exploded.split(".")
    return f"{parts[0]}.{parts[1]}"


def isp_name_of(ip: str, isp_info: dict | None) -> str:
    """Return the ISP (or org) name known for an IP, or "" when unknown."""
    info = (isp_info or {}).get(ip) or {}
    return info.get("isp", "") or info.get("org", "") or ""


def _subnet_parts(ip: str, config: DeviceCountingConfig, isp_info: dict | None) -> tuple:
    """
    Return the grouping key of an IP when subnet grouping is enabled.

    In "/16" mode the ISP has to be known, otherwise a whole /16 of unrelated
    customers would collapse into one device; without it the address falls back
    to the narrower /24 key.
    """
    if config.subnet_grouping_mode == "/16":
        ip_isp = isp_name_of(ip, isp_info)
        if ip_isp:
            return ("SUBNET_ISP_GROUP", wide_subnet_key(ip), ip_isp)
    return ("SUBNET_GROUP", subnet_key(ip))


def device_key(
    conn: ConnectionInfo,
    config: DeviceCountingConfig,
    apply_high_trust: bool = False,
    isp_info: dict | None = None,
) -> tuple | None:
    """
    Return the identity of the device behind a single connection.

    Two connections that map to the same key are the same device. ``None`` means
    the connection must not be counted at all.

    Args:
        conn: One connection of the user.
        config: Counting and grouping rules.
        apply_high_trust: Whether High Trust grouping is active for this user.
        isp_info: ``{ip: isp_info}`` used by "/16" subnet grouping.

    Returns:
        tuple | None: The device key, or None for connections that are ignored.
    """
    if conn.node_id in config.disabled_nodes:
        return None

    # A CDN presents many addresses for one client, so the node (or the inbound
    # when only the inbound is behind a CDN) counts as a single device.
    if conn.node_id in config.cdn_nodes:
        return ("CDN_NODE", conn.node_id)
    if conn.inbound_protocol in config.cdn_inbounds:
        return ("CDN_INBOUND", conn.inbound_protocol)

    if config.count_mode == COUNT_MODE_IP:
        # One client IP is one device, whatever it connects to. Subnet grouping
        # still applies, so a whole /24 (or /16 + ISP) collapses into one.
        if config.subnet_ip_grouping:
            return _subnet_parts(conn.ip, config, isp_info)
        return ("IP_ONLY", conn.ip)

    if apply_high_trust:
        # Users who have built trust: every IP reaching the same inbound is one
        # device, which is what WiFi/mobile switching looks like.
        return ("HIGH_TRUST", conn.inbound_protocol)

    if config.subnet_ip_grouping:
        return _subnet_parts(conn.ip, config, isp_info) + (conn.inbound_protocol,)

    return (conn.ip, conn.inbound_protocol)


def _connections_of(user: UserType | None) -> list[ConnectionInfo]:
    """Return a user's connections, or an empty list when there is no info."""
    if not user or not user.device_info or not user.device_info.connections:
        return []
    return user.device_info.connections


def count_devices(
    user: UserType | None,
    config: DeviceCountingConfig | None = None,
    trust_score: float = 0.0,
    isp_info: dict | None = None,
    fallback_count: int = 0,
) -> int:
    """
    Count the distinct devices of one user.

    Args:
        user: The user whose connections are counted.
        config: Counting and grouping rules; defaults are used when omitted.
        trust_score: Current trust score, for High Trust grouping.
        isp_info: ``{ip: isp_info}`` used by "/16" subnet grouping.
        fallback_count: Returned when there is no connection information at all
            (log or API collection gave IPs but no inbound/node detail); the
            caller passes the user's unique-IP count there.

    Returns:
        int: Number of distinct devices.
    """
    if config is None:
        config = DeviceCountingConfig()

    connections = _connections_of(user)
    if not connections:
        return fallback_count

    apply_high_trust = (
        config.high_trust_ip_grouping and trust_score >= config.high_trust_threshold
    )

    unique_devices = set()
    for conn in connections:
        key = device_key(
            conn, config, apply_high_trust=apply_high_trust, isp_info=isp_info
        )
        if key is not None:
            unique_devices.add(key)
    return len(unique_devices)


def build_ip_details(user_info: EnhancedUserInfo, original_user: UserType | None) -> list[str]:
    """
    Render one display line per IP with its node and inbound(s).

    Args:
        user_info: Enhanced user info holding the ISP-formatted IP strings.
        original_user: The user's raw connection data.

    Returns:
        list[str]: Display lines, empty when there is no connection info.
    """
    connections = _connections_of(original_user)
    if not connections:
        return []

    ip_to_connections: dict[str, list[ConnectionInfo]] = {}
    for conn in connections:
        if conn.ip in user_info.user.ip:
            ip_to_connections.setdefault(conn.ip, []).append(conn)

    # Map the raw address back to its "1.2.3.4 (ISP, Country)" rendering.
    raw_to_formatted = {}
    for formatted_ip in user_info.formatted_ips:
        if " (" in formatted_ip:
            raw_ip = formatted_ip.split(" (")[0]
        else:
            raw_ip = formatted_ip.split(" ")[0]
        raw_to_formatted[raw_ip] = formatted_ip

    ip_details = []
    for ip, ip_connections in ip_to_connections.items():
        formatted_ip = raw_to_formatted.get(ip, ip)
        unique_inbounds = list({c.inbound_protocol for c in ip_connections})
        first = ip_connections[0]
        node_info = f"{first.node_name}({first.node_id})"

        if len(unique_inbounds) == 1:
            ip_details.append(f"  • {formatted_ip} → {node_info} | {unique_inbounds[0]}")
        else:
            # Multiple inbounds on the same IP = multiple devices in "device" mode
            inbounds_str = ", ".join(unique_inbounds)
            ip_details.append(f"  • {formatted_ip} → {node_info} | [{inbounds_str}]")

    return ip_details


def count_devices_and_details(
    user_info: EnhancedUserInfo,
    original_user: UserType | None,
    show_enhanced_details: bool = False,
    device_config: DeviceCountingConfig | None = None,
    user_trust_score: float = 0.0,
    isp_info: dict | None = None,
) -> tuple[list[str], int]:
    """
    Count devices and optionally render the per-IP display lines in one pass.

    Returns:
        tuple[list[str], int]: (display lines, device count). The lines are empty
        when ``show_enhanced_details`` is False. With no connection information
        the count falls back to the number of formatted IPs.
    """
    if device_config is None:
        device_config = DeviceCountingConfig()

    if not _connections_of(original_user):
        return [], len(user_info.formatted_ips)

    device_count = count_devices(
        original_user,
        device_config,
        trust_score=user_trust_score,
        isp_info=isp_info,
    )
    if not show_enhanced_details:
        return [], device_count

    return build_ip_details(user_info, original_user), device_count


def _display_subnet_key(ip: str) -> str:
    """Return the "1.2.3.x" label of an IPv4 address, or the address itself."""
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if ip_obj.version != 4:
        return str(ip_obj)  # IPv6 is rare in CDN scenarios; keep it whole
    return f"{subnet_key(ip)}.x"


def group_ips_by_subnet(ip_list: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """
    Group IPs by their /24 subnet for display.

    Up to two addresses in the same subnet are shown individually; beyond that
    they collapse into "1.2.3.x (count)" so a CDN cannot flood the report.

    Args:
        ip_list (list[str]): List of IP addresses

    Returns:
        tuple[list[str], dict[str, list[str]]]:
            - List of formatted subnet representations
            - Dictionary mapping formatted representations to actual IPs
    """
    subnet_groups: dict[str, list[str]] = {}
    for ip in ip_list:
        subnet_groups.setdefault(_display_subnet_key(ip), []).append(ip)

    formatted_results = []
    ip_mapping: dict[str, list[str]] = {}
    for group_key, ips in subnet_groups.items():
        if len(ips) <= 2:
            for ip in ips:
                formatted_results.append(ip)
                ip_mapping[ip] = [ip]
        else:
            formatted_subnet = f"{group_key} ({len(ips)})"
            formatted_results.append(formatted_subnet)
            ip_mapping[formatted_subnet] = ips

    return formatted_results, ip_mapping

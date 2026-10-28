"""
This module contains the data classes used in the application.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


@dataclass(slots=True)
class PanelType:
    """Panel connection settings with an API key excluded from representations."""

    panel_domain: str
    panel_api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        """Normalize settings and reject keys that cannot safely become headers."""
        if not isinstance(self.panel_domain, str) or not self.panel_domain.strip():
            raise ValueError("PANEL_DOMAIN must be configured")
        self.panel_domain = self.panel_domain.strip()
        if not isinstance(self.panel_api_key, str) or not self.panel_api_key.strip():
            raise ValueError("PANEL_API_KEY must be configured")
        self.panel_api_key = self.panel_api_key.strip()
        if any(not 33 <= ord(character) <= 126 for character in self.panel_api_key):
            raise ValueError("PANEL_API_KEY must contain only visible ASCII characters")

    @classmethod
    def from_config(cls, config: dict) -> "PanelType":
        """Build connection settings from the canonical full configuration."""
        panel = config.get("panel", {})
        return cls(panel_domain=panel.get("domain"), panel_api_key=panel.get("api_key"))


@dataclass(slots=True)
class NodeType:
    """
    A class used to represent the data for a node.

    Attributes:
        node_id (int): The ID of the node.
        node_name (str): The name of the node.
        node_ip (str): The IP address of the node.
        status (str): The status of the node.
        message (str): The message of the node.
    """

    node_id: int
    node_name: str
    node_ip: str
    status: str
    message: str | None = None


class UserStatus(Enum):
    """
    Enum representing the type of UserStatus.

    Attributes:
        ACTIVE (str)
        DISABLE (str)
    """

    ACTIVE = "ACTIVE"
    DISABLE = "DISABLE"


@dataclass(slots=True)
class ConnectionInfo:
    """
    Represents connection information for a specific IP address.

    Attributes:
        ip (str): The IP address
        node_id (int): The node ID where this IP was seen
        node_name (str): The node name where this IP was seen
        inbound_protocol (str): The inbound protocol used (e.g., "Vless Direct", "Vmess", etc.)
        last_seen (float): Timestamp of last activity
        connection_count (int): Number of connections seen
    """

    ip: str
    node_id: int
    node_name: str
    inbound_protocol: str
    last_seen: float
    connection_count: int = 1


@dataclass(slots=True)
class DeviceInfo:
    """
    Represents device information based on IP and connection patterns.

    Attributes:
        connections (List[ConnectionInfo]): List of connection information
        is_multi_device (bool): Whether this user appears to use multiple devices
        unique_ips (set): Set of unique IP addresses
        unique_nodes (set): Set of unique node IDs
        inbound_protocols (set): Set of inbound protocols used
    """

    connections: List[ConnectionInfo] = field(default_factory=list)
    is_multi_device: bool = False
    unique_ips: set = field(default_factory=set)
    unique_nodes: set = field(default_factory=set)
    inbound_protocols: set = field(default_factory=set)


@dataclass(slots=True)
class UserType:
    """
    Represents a user type with panel API fields.

    Attributes:
        name (str): The username.
        status (UserStatus | None): The user status (local). None if no status is provided.
        ip (list[str] | list): List of IP addresses of the user.
        isp_info (dict | None): ISP information for the user's IPs.
        device_info (DeviceInfo): Device and connection information for the user.
        panel_status (str | None): Status from panel API (active/disabled/limited/expired/on_hold).
        data_limit (int | None): Data limit in bytes from panel.
        used_traffic (int | None): Used traffic in bytes from panel.
        lifetime_used_traffic (int | None): Lifetime used traffic in bytes.
        expire (str | int | None): Expiration datetime/timestamp from panel.
        group_ids (list[int] | None): Group IDs the user belongs to.
        online_at (str | None): Last online datetime from panel.
        admin_username (str | None): Admin username who owns this user.
    """

    name: str
    status: UserStatus | None = None
    ip: list[str] | list = field(default_factory=list)
    isp_info: dict | None = None
    device_info: DeviceInfo = field(default_factory=DeviceInfo)
    # Panel API fields
    panel_status: str | None = None
    data_limit: int | None = None
    used_traffic: int | None = None
    lifetime_used_traffic: int | None = None
    expire: str | int | None = None
    group_ids: list[int] | None = None
    online_at: str | None = None
    admin_username: str | None = None
    # Pre-computed filter & limit fields
    is_monitored: bool = True
    effective_ip_limit: int | None = None


@dataclass(slots=True)
class EnhancedUserInfo:
    """
    Enhanced user information with ISP details and warning status.

    Attributes:
        user (UserType): Basic user information
        formatted_ips (list[str]): List of IPs formatted with ISP info
        is_being_monitored (bool): Whether user is currently being monitored
        warning_time_remaining (int): Remaining monitoring time in seconds
    """

    user: UserType
    formatted_ips: list[str] = field(default_factory=list)
    is_being_monitored: bool = False
    warning_time_remaining: int = 0

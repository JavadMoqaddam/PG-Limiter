"""Subnet cache keys must be canonical, so equivalent IPv6 spellings collapse to one
key and malformed input does not crash the lookup."""

from db.crud.subnet_isp import SubnetISPCRUD


def test_ipv4_subnet_is_slash_24():
    assert SubnetISPCRUD.get_subnet_from_ip("192.168.1.100") == "192.168.1.0/24"


def test_equivalent_ipv6_spellings_map_to_one_subnet():
    compressed = SubnetISPCRUD.get_subnet_from_ip("2a01:5ec0::1")
    expanded = SubnetISPCRUD.get_subnet_from_ip("2a01:5ec0:0:0:0:0:0:1")
    assert compressed == expanded
    # A canonical /64 network, not a naive colon-split.
    assert compressed.endswith("/64")
    assert "::" not in compressed.split("/")[0] or compressed.count(":") <= 8


def test_ipv6_subnet_ignores_host_bits():
    a = SubnetISPCRUD.get_subnet_from_ip("2a01:5ec0:5011:9962:dead:beef:1:2")
    b = SubnetISPCRUD.get_subnet_from_ip("2a01:5ec0:5011:9962:0:0:0:99")
    assert a == b == "2a01:5ec0:5011:9962::/64"


def test_invalid_input_does_not_crash():
    assert SubnetISPCRUD.get_subnet_from_ip("not-an-ip") == "not-an-ip"

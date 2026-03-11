"""Unit tests for data models."""

from models import Host, IPAddress, Interface, DesiredState


def test_ip_address_cidr():
    ip = IPAddress(address="192.168.1.10", prefix=24)
    assert ip.cidr == "192.168.1.10/24"


def test_ip_address_is_private_lan():
    assert IPAddress(address="192.168.1.10").is_private_lan is True
    assert IPAddress(address="10.0.0.1").is_private_lan is False
    assert IPAddress(address="172.16.0.1").is_private_lan is False


def test_host_get_preferred_ip_prefers_192():
    host = Host(
        name="test",
        host_type="device",
        interfaces=[
            Interface(name="eth0", ip_addresses=[
                IPAddress(address="10.0.0.5", prefix=24),
                IPAddress(address="192.168.1.10", prefix=24),
            ]),
        ],
    )
    assert host.get_preferred_ip() == "192.168.1.10"


def test_host_get_preferred_ip_fallback():
    host = Host(
        name="test",
        host_type="device",
        interfaces=[
            Interface(name="eth0", ip_addresses=[
                IPAddress(address="10.0.0.5", prefix=24),
            ]),
        ],
    )
    assert host.get_preferred_ip() == "10.0.0.5"


def test_host_get_preferred_ip_none():
    host = Host(name="test", host_type="device")
    assert host.get_preferred_ip() is None


def test_host_get_all_ips():
    host = Host(
        name="test",
        host_type="device",
        interfaces=[
            Interface(name="eth0", ip_addresses=[
                IPAddress(address="10.0.0.1"),
                IPAddress(address="192.168.1.5"),
            ]),
            Interface(name="eth1", ip_addresses=[
                IPAddress(address="172.16.0.1"),
            ]),
        ],
    )
    assert host.get_all_ips() == ["10.0.0.1", "192.168.1.5", "172.16.0.1"]


def test_desired_state_build_ip_index():
    state = DesiredState()
    state.hosts["srv1"] = Host(
        name="srv1",
        host_type="device",
        interfaces=[
            Interface(name="eth0", ip_addresses=[
                IPAddress(address="192.168.1.10"),
                IPAddress(address="10.0.0.1"),
            ]),
        ],
    )
    state.build_ip_index()
    assert state.ip_index["192.168.1.10"] == "srv1"
    assert state.ip_index["10.0.0.1"] == "srv1"

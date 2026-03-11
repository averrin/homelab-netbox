"""Unit tests for the merger module."""

from models import Host, IPAddress, Interface, Service
from merger import merge


def test_merge_primary_hosts():
    """Proxmox and Coolify hosts become primary entries."""
    pve = [Host(name="pve-node", host_type="device", source="proxmox")]
    cool = [Host(name="cool-srv", host_type="device", source="coolify")]

    state = merge(proxmox_hosts=pve, coolify_hosts=cool)

    assert "pve-node" in state.hosts
    assert "cool-srv" in state.hosts
    assert len(state.hosts) == 2


def test_merge_pulse_enriches_by_name():
    """Pulse host with same name enriches existing host."""
    coolify_hosts = [
        Host(
            name="my-server",
            host_type="device",
            source="coolify",
            status="active",
            description="Coolify server",
            interfaces=[Interface(name="eth0", ip_addresses=[
                IPAddress(address="192.168.1.10"),
            ])],
        )
    ]
    pulse_hosts = [
        Host(
            name="my-server",
            host_type="container",
            source="pulse",
            status="active",
            description="Pulse container",
        )
    ]

    state = merge(coolify_hosts=coolify_hosts, pulse_hosts=pulse_hosts)

    assert len(state.hosts) == 1
    host = state.hosts["my-server"]
    assert host.source == "coolify"  # primary source preserved
    assert "Pulse container" in host.description


def test_merge_pulse_enriches_by_ip():
    """Pulse host matched by IP enriches existing host."""
    proxmox_hosts = [
        Host(
            name="pve-vm",
            host_type="vm",
            source="proxmox",
            interfaces=[Interface(name="eth0", ip_addresses=[
                IPAddress(address="192.168.1.50"),
            ])],
        )
    ]
    pulse_hosts = [
        Host(
            name="different-name",
            host_type="container",
            source="pulse",
            interfaces=[Interface(name="eth0", ip_addresses=[
                IPAddress(address="192.168.1.50"),
            ])],
        )
    ]

    state = merge(proxmox_hosts=proxmox_hosts, pulse_hosts=pulse_hosts)

    assert len(state.hosts) == 1
    assert "pve-vm" in state.hosts


def test_merge_pulse_new_host():
    """Pulse host with no match becomes a new entry."""
    pulse_hosts = [
        Host(name="orphan-container", host_type="container", source="pulse")
    ]

    state = merge(pulse_hosts=pulse_hosts)

    assert "orphan-container" in state.hosts


def test_merge_npm_attaches_by_ip():
    """NPM service attaches to host matched by forward_host IP."""
    coolify_hosts = [
        Host(
            name="web-server",
            host_type="device",
            source="coolify",
            interfaces=[Interface(name="eth0", ip_addresses=[
                IPAddress(address="192.168.1.20"),
            ])],
        )
    ]
    npm_services = [
        Service(
            name="NPM Proxy - example.com",
            ports=[443],
            forward_host="192.168.1.20",
            external_urls=["https://example.com"],
        )
    ]

    state = merge(coolify_hosts=coolify_hosts, npm_services=npm_services)

    host = state.hosts["web-server"]
    assert len(host.services) == 1
    assert host.services[0].name == "NPM Proxy - example.com"
    assert len(state.unmatched_services) == 0


def test_merge_npm_unmatched():
    """NPM service with no matching host goes to unmatched_services."""
    npm_services = [
        Service(name="orphan-svc", ports=[80], forward_host="10.99.99.99")
    ]

    state = merge(npm_services=npm_services)

    assert len(state.unmatched_services) == 1


def test_merge_ip_preference_192():
    """When Pulse host has multiple IPs, 192.168.x.x is used for matching."""
    proxmox_hosts = [
        Host(
            name="pve-vm",
            host_type="vm",
            source="proxmox",
            interfaces=[Interface(name="eth0", ip_addresses=[
                IPAddress(address="192.168.1.100"),
            ])],
        )
    ]
    pulse_hosts = [
        Host(
            name="some-other-name",
            host_type="container",
            source="pulse",
            interfaces=[Interface(name="eth0", ip_addresses=[
                IPAddress(address="10.0.0.99"),
                IPAddress(address="192.168.1.100"),
            ])],
        )
    ]

    state = merge(proxmox_hosts=proxmox_hosts, pulse_hosts=pulse_hosts)

    # Should match by the 192.168.x.x IP
    assert len(state.hosts) == 1
    assert "pve-vm" in state.hosts


def test_merge_npm_enriches_by_domain():
    """NPM service matched by domain enriches Coolify host (IP + external_urls)."""
    coolify_hosts = [
        Host(
            name="my-app",
            host_type="vm",
            source="coolify",
            custom_fields={"domains": ["app.example.com"]},
        )
    ]
    npm_services = [
        Service(
            name="NPM Proxy - app.example.com",
            ports=[443],
            forward_host="192.168.0.133",
            description="External: app.example.com → Internal: http://192.168.0.133:8050",
            external_urls=["https://app.example.com"],
        )
    ]

    state = merge(coolify_hosts=coolify_hosts, npm_services=npm_services)

    host = state.hosts["my-app"]
    # No separate service — data absorbed into host
    assert len(host.services) == 0
    # Forward IP added as interface
    assert "192.168.0.133" in host.get_all_ips()
    # External URLs accumulated in custom_fields
    assert "https://app.example.com" in host.custom_fields["external_urls"]
    assert len(state.unmatched_services) == 0


def test_merge_npm_domain_match_multiple_urls():
    """NPM service with multiple domains matches if any domain matches Coolify host."""
    coolify_hosts = [
        Host(
            name="api-server",
            host_type="vm",
            source="coolify",
            custom_fields={"domains": ["api.example.com"]},
        )
    ]
    npm_services = [
        Service(
            name="NPM Proxy - www.example.com",
            ports=[443],
            forward_host="172.17.0.5",
            description="External: www.example.com, api.example.com → Internal: http://172.17.0.5:443",
            external_urls=["https://www.example.com", "https://api.example.com"],
        )
    ]

    state = merge(coolify_hosts=coolify_hosts, npm_services=npm_services)

    host = state.hosts["api-server"]
    assert len(host.services) == 0  # absorbed, not a separate service
    assert "172.17.0.5" in host.get_all_ips()
    assert "https://www.example.com" in host.custom_fields["external_urls"]
    assert "https://api.example.com" in host.custom_fields["external_urls"]
    assert len(state.unmatched_services) == 0


def test_merge_npm_multiple_proxies_same_ip():
    """Multiple NPM proxies forwarding to same IP produce one IP, merged URLs."""
    coolify_hosts = [
        Host(
            name="my-app",
            host_type="vm",
            source="coolify",
            custom_fields={"domains": ["app.example.com", "api.example.com"]},
        )
    ]
    npm_services = [
        Service(
            name="NPM Proxy - app.example.com",
            ports=[443],
            forward_host="192.168.0.133",
            external_urls=["https://app.example.com"],
        ),
        Service(
            name="NPM Proxy - api.example.com",
            ports=[443],
            forward_host="192.168.0.133",
            external_urls=["https://api.example.com"],
        ),
        Service(
            name="NPM Proxy - internal traefik",
            ports=[80],
            forward_host="192.168.0.133",
            external_urls=["https://traefik.example.com"],
        ),
    ]

    state = merge(coolify_hosts=coolify_hosts, npm_services=npm_services)

    host = state.hosts["my-app"]
    # Only one IP, not three
    assert host.get_all_ips().count("192.168.0.133") == 1
    # Two domain-matched URLs accumulated (traefik has no domain match → unmatched)
    assert len(host.custom_fields["external_urls"]) == 2
    assert "https://app.example.com" in host.custom_fields["external_urls"]
    assert "https://api.example.com" in host.custom_fields["external_urls"]
    assert len(host.services) == 0
    assert len(state.unmatched_services) == 1  # traefik has no domain match


def test_merge_npm_ip_match_keeps_service():
    """IP-based match keeps NPM as a separate service (not absorbed)."""
    proxmox_hosts = [
        Host(
            name="pve-web",
            host_type="vm",
            source="proxmox",
            interfaces=[Interface(name="eth0", ip_addresses=[
                IPAddress(address="192.168.1.20"),
            ])],
        )
    ]
    npm_services = [
        Service(
            name="NPM Proxy - web.example.com",
            ports=[443],
            forward_host="192.168.1.20",
            external_urls=["https://web.example.com"],
        )
    ]

    state = merge(proxmox_hosts=proxmox_hosts, npm_services=npm_services)

    # IP match → kept as a service
    assert len(state.hosts["pve-web"].services) == 1
    assert len(state.unmatched_services) == 0


def test_merge_npm_ip_match_takes_priority_over_domain():
    """IP-based match is preferred over domain match."""
    proxmox_hosts = [
        Host(
            name="pve-web",
            host_type="vm",
            source="proxmox",
            interfaces=[Interface(name="eth0", ip_addresses=[
                IPAddress(address="192.168.1.20"),
            ])],
        )
    ]
    coolify_hosts = [
        Host(
            name="cool-web",
            host_type="vm",
            source="coolify",
            custom_fields={"domains": ["web.example.com"]},
        )
    ]
    npm_services = [
        Service(
            name="NPM Proxy - web.example.com",
            ports=[443],
            forward_host="192.168.1.20",
            external_urls=["https://web.example.com"],
        )
    ]

    state = merge(
        proxmox_hosts=proxmox_hosts,
        coolify_hosts=coolify_hosts,
        npm_services=npm_services,
    )

    # Should attach to Proxmox host by IP as a service, not enrich Coolify host
    assert len(state.hosts["pve-web"].services) == 1
    assert len(state.hosts["cool-web"].services) == 0
    assert len(state.unmatched_services) == 0


def test_merge_npm_no_domain_no_ip_unmatched():
    """NPM service with no IP or domain match goes to unmatched."""
    coolify_hosts = [
        Host(
            name="my-app",
            host_type="vm",
            source="coolify",
            custom_fields={"domains": ["app.example.com"]},
        )
    ]
    npm_services = [
        Service(
            name="NPM Proxy - other.example.com",
            ports=[443],
            forward_host="10.99.99.99",
            external_urls=["https://other.example.com"],
        )
    ]

    state = merge(coolify_hosts=coolify_hosts, npm_services=npm_services)

    assert len(state.hosts["my-app"].services) == 0
    assert len(state.unmatched_services) == 1


def test_merge_empty():
    """Merging nothing produces empty state."""
    state = merge()
    assert len(state.hosts) == 0
    assert len(state.unmatched_services) == 0

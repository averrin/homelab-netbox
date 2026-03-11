"""Unified data models for the Desired-State Reconciler."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IPAddress:
    address: str  # e.g. "192.168.1.10"
    prefix: int = 24
    source: str = ""

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"

    @property
    def is_private_lan(self) -> bool:
        """True if address is in 192.168.x.x range (preferred for matching)."""
        return self.address.startswith("192.168.")


@dataclass
class Interface:
    name: str  # e.g. "eth0", "net0"
    mac_address: str | None = None
    ip_addresses: list[IPAddress] = field(default_factory=list)


@dataclass
class Disk:
    name: str
    size_mb: int
    has_backup: bool = True


@dataclass
class Host:
    name: str
    host_type: str  # "device" | "vm" | "container"
    status: str = "active"  # "active" | "offline"
    source: str = ""  # primary source: "proxmox", "coolify", "pulse"
    description: str = ""
    interfaces: list[Interface] = field(default_factory=list)
    disks: list[Disk] = field(default_factory=list)
    
    # Metadata URLs
    config_url: str | None = None
    external_url: str | None = None
    internal_url: str | None = None
    
    # Proxmox-specific
    vmid: int | None = None
    vcpus: float | None = None
    memory_mb: int | None = None
    platform: str | None = None  # "qemu", "lxc"
    cluster_name: str | None = None
    tags: list[str] = field(default_factory=list)
    custom_fields: dict = field(default_factory=dict)
    port: int | None = None
    
    # Site / role hints
    site_name: str | None = None
    role_name: str | None = None
    netbox_sync_protected: bool = False

    def get_preferred_ip(self) -> str | None:
        """Return the best IP for matching: prefer 192.168.x.x, then first available."""
        all_ips: list[IPAddress] = []
        for iface in self.interfaces:
            all_ips.extend(iface.ip_addresses)
        if not all_ips:
            return None
        # Prefer 192.168.x.x
        for ip in all_ips:
            if ip.is_private_lan:
                return ip.address
        return all_ips[0].address

    def get_all_ips(self) -> list[str]:
        """Return all IP addresses across all interfaces."""
        ips = []
        for iface in self.interfaces:
            for ip in iface.ip_addresses:
                ips.append(ip.address)
        return ips


@dataclass
class Service:
    """[DEPRECATED] Placeholder for backward compatibility during model migration."""
    name: str
    protocol: str = "tcp"
    ports: list[int] = field(default_factory=list)
    description: str = ""
    external_urls: list[str] = field(default_factory=list)
    internal_urls: list[str] = field(default_factory=list)
    forward_host: str | None = None


@dataclass
class DesiredState:
    hosts: dict[str, Host] = field(default_factory=dict)  # keyed by canonical name
    ip_index: dict[str, str] = field(default_factory=dict)  # ip -> host name

    def build_ip_index(self):
        """Rebuild the IP→host-name index from all hosts."""
        self.ip_index.clear()
        for name, host in self.hosts.items():
            for ip in host.get_all_ips():
                self.ip_index[ip] = name


@dataclass
class Action:
    verb: str  # "create" | "update" | "skip" | "delete"
    object_type: str  # "device" | "vm" | "interface" | "ip" | "disk"
    target: str  # human-readable identifier
    details: dict = field(default_factory=dict)  # fields being set/changed
    reason: str = ""

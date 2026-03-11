"""Proxmox collector — fetches nodes, VMs, and LXC containers from Proxmox VE.

Ported from netbox-pve-sync with adaptations to produce our unified Host model.
"""

from __future__ import annotations

import re
from typing import Optional

from proxmoxer import ProxmoxAPI, ResourceException

from config import ProxmoxConfig
from models import Disk, Host, IPAddress, Interface


def collect(cfg: ProxmoxConfig) -> list[Host]:
    """Connect to Proxmox and return Host objects for nodes, VMs, and containers.

    Raises:
        RuntimeError: If credentials are missing.
    """
    if not cfg or not cfg.host or not cfg.user or not cfg.token_name or not cfg.token_secret:
        raise RuntimeError(
            "Proxmox collector requires PVE_API_HOST, PVE_API_USER, PVE_API_TOKEN, and PVE_API_SECRET."
        )

    pve = ProxmoxAPI(
        host=cfg.host,
        user=cfg.user,
        token_name=cfg.token_name,
        token_value=cfg.token_secret,
        verify_ssl=False,
    )

    hosts: list[Host] = []

    # Fetch VM-level metadata (tags, pools, HA, replication)
    vm_tags: dict[int, list[str]] = {}
    vm_raw_tags: dict[int, list[str]] = {}
    for res in pve.cluster.resources.get(type="vm"):
        vmid = res["vmid"]
        vm_tags[vmid] = []
        vm_raw_tags[vmid] = []
        if "pool" in res:
            vm_tags[vmid].append(f"Pool/{res['pool']}")
        if "tags" in res:
            raw_t = res["tags"].split(";")
            ip_pattern = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
            cleaned_tags = [
                t for t in raw_t
                if not (ip_pattern.match(t) and not t.startswith("192.168."))
            ]
            vm_raw_tags[vmid] = cleaned_tags
            vm_tags[vmid].extend(cleaned_tags)

    ha_vmids = set()
    try:
        for r in pve.cluster.ha.status.current.get():
            if r["type"] == "service":
                ha_vmids.add(int(r["sid"].split(":")[1]))
    except Exception:
        pass

    # Process each node
    for pve_node in pve.nodes.get():
        node_name = pve_node["node"]
        node_status = "active" if pve_node["status"] == "online" else "offline"

        # Proxmox Config URL for the node
        clean_host = cfg.host.split(":")[0]
        config_url = f"https://{clean_host}:8006"

        # Emit the node itself as a device
        hosts.append(
            Host(
                name=node_name,
                host_type="device",
                status=node_status,
                source="proxmox",
                description=f"Proxmox node {node_name}",
                role_name="Server",
                platform="proxmox",
                cluster_name=cfg.cluster_name,
                config_url=config_url,
            )
        )

        # Replication info for this node
        replicated_vmids = set()
        try:
            for r in pve.nodes(node_name).replication.get():
                replicated_vmids.add(r["guest"])
        except Exception:
            pass

        # QEMU VMs
        for vm in pve.nodes(node_name).qemu.get():
            host = _process_qemu_vm(
                pve, node_name, vm, cfg,
                vm_tags.get(vm["vmid"], []),
                vm["vmid"] in replicated_vmids,
                vm["vmid"] in ha_vmids,
            )
            if host:
                hosts.append(host)

        # LXC containers
        for ct in pve.nodes(node_name).lxc.get():
            host = _process_lxc_container(
                pve, node_name, ct, cfg,
                vm_tags.get(ct["vmid"], []),
                vm_raw_tags.get(ct["vmid"], []),
                ct["vmid"] in replicated_vmids,
                ct["vmid"] in ha_vmids,
            )
            if host:
                hosts.append(host)

    return hosts


def _process_qemu_vm(
    pve: ProxmoxAPI,
    node_name: str,
    vm: dict,
    cfg: ProxmoxConfig,
    tags: list[str],
    is_replicated: bool,
    has_ha: bool,
) -> Host | None:
    vmid = vm["vmid"]
    try:
        config = pve.nodes(node_name).qemu(vmid).config.get()
    except Exception:
        return None

    # Get agent network interfaces
    ip_addresses_by_iface: dict[str, list[dict]] = {}
    try:
        agent_ifaces = pve.nodes(node_name).qemu(vmid).agent("network-get-interfaces").get()
        for result in agent_ifaces.get("result", []):
            ip_addresses_by_iface[result["name"]] = result.get("ip-addresses", [])
    except (ResourceException, Exception):
        pass

    vcpus = config.get("vcpus", config.get("cores", 1) * config.get("sockets", 1))
    memory = int(config.get("memory", 512))

    # Proxmox Config URL: https://<proxmox_host>:8006/#v1:0:=qemu%2F<vmid>
    # cfg.host might already have :8006, so we split it to be safe
    clean_host = cfg.host.split(":")[0]
    config_url = f"https://{clean_host}:8006/#v1:0:=qemu%2F{vmid}"

    host = Host(
        name=vm["name"],
        host_type="vm",
        status="active" if vm["status"] == "running" else "offline",
        source="proxmox",
        vmid=vmid,
        vcpus=vcpus,
        memory_mb=memory,
        platform="qemu",
        cluster_name=cfg.cluster_name,
        tags=tags,
        config_url=config_url,
        custom_fields={
            "autostart": config.get("onboot") == 1,
            "replicated": is_replicated,
            "ha": has_ha,
            "vmid": vmid,
        },
    )

    # Interfaces
    host.interfaces = _extract_qemu_interfaces(config, ip_addresses_by_iface)

    # Disks
    host.disks = _extract_qemu_disks(config)

    return host


def _process_lxc_container(
    pve: ProxmoxAPI,
    node_name: str,
    ct: dict,
    cfg: ProxmoxConfig,
    tags: list[str],
    raw_tags: list[str],
    is_replicated: bool,
    has_ha: bool,
) -> Host | None:
    vmid = ct["vmid"]
    try:
        config = pve.nodes(node_name).lxc(vmid).config.get()
    except Exception:
        return None

    tag_ip = _extract_ip_from_tags(raw_tags)

    # Proxmox Config URL: https://<proxmox_host>:8006/#v1:0:=lxc%2F<vmid>
    clean_host = cfg.host.split(":")[0]
    config_url = f"https://{clean_host}:8006/#v1:0:=lxc%2F{vmid}"
    internal_url = f"http://{tag_ip}" if tag_ip else None

    host = Host(
        name=ct["name"],
        host_type="vm",  # LXC containers are VMs in NetBox terminology
        status="active" if ct["status"] == "running" else "offline",
        source="proxmox",
        vmid=vmid,
        vcpus=config.get("cores", 1),
        memory_mb=int(config.get("memory", 512)),
        platform="lxc",
        cluster_name=cfg.cluster_name,
        tags=tags,
        config_url=config_url,
        internal_url=internal_url,
        custom_fields={
            "autostart": config.get("onboot") == 1,
            "replicated": is_replicated,
            "ha": has_ha,
            "vmid": vmid,
        },
    )

    # Interfaces
    host.interfaces = _extract_lxc_interfaces(config, raw_tags)

    # Disks
    host.disks = _extract_lxc_disks(config)

    return host


# ---------------------------------------------------------------------------
# Network interface extraction
# ---------------------------------------------------------------------------

def _parse_kv_string(raw: str) -> dict[str, str]:
    """Parse comma-separated key=value strings like PVE network/disk definitions."""
    result = {}
    for component in raw.split(","):
        parts = component.split("=", 1)
        if len(parts) == 2:
            result[parts[0]] = parts[1]
        else:
            result["name"] = parts[0]
    return result


def _extract_qemu_interfaces(config: dict, ip_by_iface: dict) -> list[Interface]:
    interfaces = []
    for key, value in config.items():
        if not key.startswith("net"):
            continue
        net = _parse_kv_string(value)

        mac = None
        for model in ("virtio", "e1000"):
            if model in net:
                mac = net[model]
                break
        if mac is None:
            continue

        iface = Interface(name=key, mac_address=mac)

        # Try to get IP from agent data
        for raw_name in ("eth0", "ens18", "ens19", key):
            if raw_name in ip_by_iface:
                for ip_info in ip_by_iface[raw_name]:
                    addr = ip_info.get("ip-address", "")
                    prefix = ip_info.get("prefix", 24)
                    if addr and ":" not in addr:  # skip IPv6
                        iface.ip_addresses.append(
                            IPAddress(address=addr, prefix=prefix, source="proxmox")
                        )
                break

        interfaces.append(iface)
    return interfaces


def _extract_ip_from_tags(raw_tags: list[str]) -> str | None:
    """Extract an IPv4 address from PVE tags."""
    pattern = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    for tag in raw_tags:
        if pattern.match(tag):
            return tag
        if "192.168." in tag:
            # Maybe the tag is something like "192.168.0.12" but didn't match perfectly
            # Let's extract exactly the IP portion if it exists in a longer string
            match = re.search(r"(192\.168\.\d{1,3}\.\d{1,3})", tag)
            if match:
                return match.group(1)
    return None


def _extract_lxc_interfaces(config: dict, raw_tags: list[str]) -> list[Interface]:
    tag_ip = _extract_ip_from_tags(raw_tags)
    interfaces = []

    for key, value in config.items():
        if not key.startswith("net"):
            continue
        net = _parse_kv_string(value)
        mac = net.get("hwaddr")
        if not mac:
            continue

        iface_name = net.get("name", key)
        iface = Interface(name=iface_name, mac_address=mac)

        # 1. Add IP from config (static)
        ip_config = net.get("ip")
        if ip_config and ip_config not in ("dhcp", "manual"):
            parts = ip_config.split("/")
            if len(parts) == 2:
                iface.ip_addresses.append(
                    IPAddress(address=parts[0], prefix=int(parts[1]), source="proxmox")
                )
        
        # 2. Add tag-based IP for net0 if found (often the 'real' internal IP)
        if tag_ip and key == "net0":
            existing_ips = {ip.address for ip in iface.ip_addresses}
            if tag_ip not in existing_ips:
                iface.ip_addresses.append(
                    IPAddress(address=tag_ip, prefix=24, source="proxmox")
                )

        interfaces.append(iface)
    return interfaces


# ---------------------------------------------------------------------------
# Disk extraction
# ---------------------------------------------------------------------------

def _parse_disk_size(raw: str) -> int:
    """Parse PVE disk size string (e.g. '32G') into MB."""
    if not raw:
        return 0
    unit = raw[-1].upper()
    try:
        size = int(raw[:-1])
    except ValueError:
        return 0
    if unit == "M":
        return size
    if unit == "G":
        return size * 1000
    if unit == "T":
        return size * 1_000_000
    return 0


def _extract_qemu_disks(config: dict) -> list[Disk]:
    disks = []
    for key, value in config.items():
        if not key.startswith("scsi") or key == "scsihw":
            continue
        d = _parse_kv_string(value)
        if "size" not in d:
            continue
        disks.append(Disk(
            name=d.get("name", key),
            size_mb=_parse_disk_size(d["size"]),
            has_backup=d.get("backup", "1") == "1",
        ))
    return disks


def _extract_lxc_disks(config: dict) -> list[Disk]:
    disks = []
    for key, value in config.items():
        if key != "rootfs" and not key.startswith("mp"):
            continue
        d = _parse_kv_string(value)
        if "size" not in d:
            continue
        disks.append(Disk(
            name=d.get("name", key),
            size_mb=_parse_disk_size(d["size"]),
            has_backup=d.get("backup", "1") == "1",
        ))
    return disks

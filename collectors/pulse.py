"""Pulse collector — fetches container resources from Pulse API.

Pulse unified resource model uses hyphenated type strings:
  "docker-container", "container" (LXC), "oci-container", etc.
IPs live in identity.ips[] and names in displayName or name.

Resource type hierarchy:
  "host"           → physical machine with Pulse agent (→ device)
  "docker-host"    → Docker daemon; id==platformId UUID, name is human label
  "docker-container" / "oci-container" / "container" / "pod" → containers

See: https://github.com/rcourtman/Pulse/blob/main/docs/API.md
"""

from __future__ import annotations

import requests

from config import SourceConfig
from models import Host, IPAddress, Interface


_CONTAINER_TYPES = {
    "docker-container",
    "oci-container",
    "container",
    "pod",
}


def collect(cfg: SourceConfig) -> list[Host]:
    """Fetch /api/resources, return Host objects for containers and standalone hosts.

    Raises:
        RuntimeError: If credentials are missing.
    """
    if not cfg or not cfg.url or not cfg.token:
        raise RuntimeError("Pulse collector requires PULSE_URL and PULSE_TOKEN.")

    headers = {"X-API-Token": cfg.token}
    url = f"{cfg.url}/api/resources"
    response = requests.get(url, headers=headers, verify=False)
    response.raise_for_status()
    resources = response.json()

    all_resources = _flatten_resources(resources)

    # Build platformId UUID → human name from docker-host entries
    platform_names: dict[str, str] = {
        r["id"]: (r.get("displayName") or r.get("name") or r["id"])
        for r in all_resources
        if r.get("type") == "docker-host" and r.get("id")
    }

    hosts: list[Host] = []

    # Standalone physical hosts (e.g. DietPi) → devices
    for res in all_resources:
        if res.get("type") != "host":
            continue
        name = res.get("displayName") or res.get("name")
        if not name:
            continue
        status_raw = res.get("status", "").lower()
        status = "active" if status_raw in ("running", "online") else "offline"
        ips_raw = _extract_ips(res)
        interfaces = []
        if ips_raw:
            ip_objs = [IPAddress(address=ip, prefix=32, source="pulse") for ip in ips_raw if ip]
            if ip_objs:
                interfaces.append(Interface(name="eth0", ip_addresses=ip_objs))
        hosts.append(Host(
            name=name,
            host_type="device",
            status=status,
            source="pulse",
            description=f"Pulse Host: {res.get('id', '')}",
            interfaces=interfaces,
        ))

    # Containers → vms (cluster derived from parent docker-host name)
    for container in all_resources:
        if not _is_container(container):
            continue
        name = (
            container.get("displayName")
            or container.get("name")
            or container.get("id")
        )
        if not name:
            continue

        status_raw = container.get("status", "").lower()
        status = "active" if status_raw in ("running", "online") else "offline"

        ips_raw = _extract_ips(container)
        interfaces = []
        if ips_raw:
            ip_objs = [IPAddress(address=ip, prefix=32, source="pulse") for ip in ips_raw if ip]
            if ip_objs:
                interfaces.append(Interface(name="eth0", ip_addresses=ip_objs))

        platform_id = container.get("platformId") or container.get("parentId")
        cluster_name = platform_names.get(platform_id) if platform_id else None

        platform_type = container.get("platformType", "")
        if platform_type == "docker":
            platform = "docker"
        elif platform_type == "proxmox-pve":
            platform = container.get("platformData", {}).get("type") or "lxc"
        else:
            platform = None

        hosts.append(Host(
            name=name,
            host_type="container",
            status=status,
            source="pulse",
            description=f"Pulse Container ID: {container.get('id', '')}",
            interfaces=interfaces,
            cluster_name=cluster_name,
            platform=platform,
        ))

    return hosts


def _extract_ips(resource: dict) -> list[str]:
    """Extract IP addresses from a Pulse resource."""
    ips = []

    # Unified model: identity.ips[]
    identity = resource.get("identity")
    if isinstance(identity, dict):
        identity_ips = identity.get("ips")
        if identity_ips:
            if isinstance(identity_ips, list):
                ips.extend(identity_ips)
            else:
                ips.append(identity_ips)

    # Platform data: ipAddresses (Proxmox LXC) or networks (Docker)
    platform_data = resource.get("platformData")
    if isinstance(platform_data, dict):
        if "ipAddresses" in platform_data:
            ips.extend(platform_data["ipAddresses"])
        if "networks" in platform_data and isinstance(platform_data["networks"], list):
            for net in platform_data["networks"]:
                if isinstance(net, dict):
                    if "ipv4" in net:
                        ips.append(net["ipv4"])
                    if "ipv6" in net:
                        ips.append(net["ipv6"])

    # Fallback: top-level ipAddresses (legacy / custom)
    ips_raw = resource.get("ipAddresses", [])
    if isinstance(ips_raw, str):
        ips.append(ips_raw)
    elif isinstance(ips_raw, list):
        ips.extend(ips_raw)

    # Strip CIDR suffixes and deduplicate
    clean_ips = []
    for ip in ips:
        if not ip:
            continue
        clean_ip = ip.split("/")[0]
        if clean_ip not in clean_ips:
            clean_ips.append(clean_ip)

    return clean_ips


def _flatten_resources(resources) -> list[dict]:
    """Normalize the Pulse API response into a flat list of resource dicts."""
    if isinstance(resources, list):
        return resources
    if isinstance(resources, dict):
        for key in ("resources", "data"):
            if key in resources:
                val = resources[key]
                return val if isinstance(val, list) else list(val.values())
        return [v for v in resources.values() if isinstance(v, dict)]
    return []


def _is_container(res: dict) -> bool:
    rt = res.get("type") or res.get("resourceType") or ""
    return rt in _CONTAINER_TYPES

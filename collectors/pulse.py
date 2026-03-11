"""Pulse collector — fetches container resources from Pulse API.

Pulse unified resource model uses hyphenated type strings:
  "docker-container", "container" (LXC), "oci-container", etc.
IPs live in identity.ips[] and names in displayName or name.

See: https://github.com/rcourtman/Pulse/blob/main/docs/API.md
"""

from __future__ import annotations

import requests

from config import SourceConfig
from models import Host, IPAddress, Interface


# Resource types we treat as containers
_CONTAINER_TYPES = {
    "docker-container",   # Docker container (current Pulse schema)
    "oci-container",      # OCI container (Proxmox VE 9.1+)
    "container",          # LXC container
    "pod",                # Kubernetes pod
}


def collect(cfg: SourceConfig) -> list[Host]:
    """Fetch /api/resources, filter containers, return Host objects.

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

    raw_containers = _filter_containers(resources)

    hosts: list[Host] = []
    for container in raw_containers:
        name = (
            container.get("displayName")
            or container.get("name")
            or container.get("id")
        )
        if not name:
            continue

        status_raw = container.get("status", "").lower()
        status = "active" if status_raw in ("running", "online") else "offline"

        # Parse IP addresses — Pulse stores them in identity.ips[]
        ips_raw = _extract_ips(container)

        interfaces = []
        if ips_raw:
            ip_objs = [
                IPAddress(address=ip, prefix=32, source="pulse")
                for ip in ips_raw if ip
            ]
            if ip_objs:
                interfaces.append(Interface(name="eth0", ip_addresses=ip_objs))

        hosts.append(
            Host(
                name=name,
                host_type="container",
                status=status,
                source="pulse",
                description=f"Pulse Container ID: {container.get('id', '')}",
                interfaces=interfaces,
                cluster_name=None,
            )
        )

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


def _filter_containers(resources) -> list[dict]:
    """Extract container-type resources from the Pulse API response."""
    containers = []

    if isinstance(resources, list):
        for res in resources:
            if _is_container(res):
                containers.append(res)
    elif isinstance(resources, dict) and "data" in resources:
        for res in resources["data"]:
            if _is_container(res):
                containers.append(res)
    elif isinstance(resources, dict) and "resources" in resources:
        for res in resources["resources"]:
            if _is_container(res):
                containers.append(res)
    elif isinstance(resources, dict):
        for _k, res in resources.items():
            if isinstance(res, dict) and _is_container(res):
                containers.append(res)

    return containers


def _is_container(res: dict) -> bool:
    """Check if a resource dict represents a container."""
    rt = res.get("type") or res.get("resourceType") or ""
    return rt in _CONTAINER_TYPES

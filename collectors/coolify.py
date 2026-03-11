"""Coolify collector — fetches applications and services from Coolify API.

Coolify has two object types:
- Applications (1 container each, custom deployments)
- Services (can have multiple containers, e.g. database + app)

Both are fetched and flattened into VM-type Host objects.

Note: Coolify server IPs (e.g. "host.docker.internal") are typically
useless for matching. Instead, we extract FQDNs which can be matched
against NPM proxy domain_names by the merger.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import requests

from config import SourceConfig
from models import Host, IPAddress, Interface


def collect(cfg: SourceConfig) -> list[Host]:
    """Fetch applications and services from Coolify, return Host objects (type=vm).

    Raises:
        RuntimeError: If credentials are missing.
    """
    if not cfg or not cfg.url or not cfg.token:
        raise RuntimeError("Coolify collector requires COOLIFY_URL and COOLIFY_TOKEN.")

    headers = {"Authorization": f"Bearer {cfg.token}"}
    hosts: list[Host] = []

    # Fetch projects to map environment_id -> (project_uuid, environment_uuid)
    env_to_proj: dict[int, tuple[str, str]] = {}
    try:
        resp = requests.get(f"{cfg.url}/api/v1/projects", headers=headers, verify=False)
        resp.raise_for_status()
        for proj_info in resp.json():
            proj_uuid = proj_info.get("uuid")
            if not proj_uuid:
                continue
                
            # Fetch detailed project info to get environments
            proj_resp = requests.get(f"{cfg.url}/api/v1/projects/{proj_uuid}", headers=headers, verify=False)
            if not proj_resp.ok:
                continue
                
            proj = proj_resp.json()
            for env in proj.get("environments", []):
                env_id = env.get("id")
                env_uuid = env.get("uuid")
                if env_id is not None and proj_uuid and env_uuid:
                    env_to_proj[env_id] = (proj_uuid, env_uuid)
    except Exception as e:
        print(f"  Warning: failed to fetch Coolify projects for config links: {e}")

    # Fetch applications
    try:
        resp = requests.get(f"{cfg.url}/api/v1/applications", headers=headers, verify=False)
        resp.raise_for_status()
        for app in resp.json():
            host = _parse_application(app, cfg.url, env_to_proj)
            if host:
                hosts.append(host)
    except Exception as e:
        print(f"  Warning: failed to fetch Coolify applications: {e}")

    # Fetch services
    try:
        resp = requests.get(f"{cfg.url}/api/v1/services", headers=headers, verify=False)
        resp.raise_for_status()
        for svc in resp.json():
            svc_hosts = _parse_service(svc, cfg.url, env_to_proj)
            hosts.extend(svc_hosts)
    except Exception as e:
        print(f"  Warning: failed to fetch Coolify services: {e}")

    return hosts


def _extract_domains(fqdn_field: str | None) -> list[str]:
    """Extract clean domain names from Coolify FQDN field.

    Coolify FQDNs can be comma-separated URLs like:
      "https://app.example.com,https://api.example.com:8080"

    Returns bare hostnames: ["app.example.com", "api.example.com"]
    """
    if not fqdn_field:
        return []
    domains = []
    for entry in fqdn_field.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parsed = urlparse(entry)
        hostname = parsed.hostname or entry
        # Skip if it looks like a bare IP or localhost
        if hostname and not _is_useless_host(hostname):
            domains.append(hostname)
            
    # Deduplicate while preserving order
    return list(dict.fromkeys(domains))


def _is_useless_host(value: str) -> bool:
    """True if the value is a docker-internal or localhost-ish hostname/IP."""
    useless = {
        "host.docker.internal",
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
    }
    return value.lower() in useless


def _extract_port(fqdn_field: str | None) -> int | None:
    """Extract first non-standard port from Coolify FQDN field."""
    if not fqdn_field:
        return None
    for entry in fqdn_field.split(","):
        entry = entry.strip()
        if not entry:
            continue
        # urlparse requires scheme for port extraction
        if "://" not in entry:
            entry = f"http://{entry}"
        parsed = urlparse(entry)
        if parsed.port:
            return parsed.port
    return None


def _parse_application(app: dict, host_url: str, env_to_proj: dict) -> Host | None:
    """Parse a Coolify application into a Host."""
    name = app.get("name")
    if not name:
        return None

    fqdn = app.get("fqdn", "") or ""
    description = app.get("description", "") or ""
    status_raw = str(app.get("status", ""))
    status = "active" if "running" in status_raw.lower() else "offline"
    domains = _extract_domains(fqdn)
    port = _extract_port(fqdn)

    if fqdn and fqdn not in description:
        description = f"{description} | FQDN: {fqdn}".strip(" | ")

    custom_fields = {}
    if domains:
        custom_fields["domains"] = domains
        
    config_url = None
    env_id = app.get("environment_id")
    app_uuid = app.get("uuid")
    if app_uuid:
        custom_fields["coolify_uuids"] = [app_uuid]
        
    if env_id is not None and env_id in env_to_proj:
        proj_uuid, env_uuid = env_to_proj[env_id]
        if proj_uuid and env_uuid and app_uuid:
            config_url = f"{host_url}/project/{proj_uuid}/environment/{env_uuid}/application/{app_uuid}"

    return Host(
        name=name,
        host_type="vm",
        status=status,
        source="coolify",
        description=description,
        platform="docker",
        cluster_name="Coolify",
        custom_fields=custom_fields,
        config_url=config_url,
        port=port,
        netbox_sync_protected=app.get("is_sync_protected", False),
    )


def _parse_service(svc: dict, host_url: str, env_to_proj: dict) -> list[Host]:
    """Parse a Coolify service into one or more Hosts."""
    # Prefer the human-readable name; fall back to the service identifier
    name = svc.get("name") or svc.get("id")
    if not name:
        return []

    description = svc.get("description", "") or ""
    status_raw = str(svc.get("status", ""))
    status = "active" if "running" in status_raw.lower() else "offline"
    fqdn_parts = []
    
    # Extract fqdn at the top-level
    if svc.get("fqdn"):
        fqdn_parts.append(svc.get("fqdn"))
        
    # Extract fqdn from nested applications within the service
    for app in svc.get("applications", []):
        if app.get("fqdn"):
            fqdn_parts.append(app.get("fqdn"))
            
    fqdn_str = ",".join([f for f in fqdn_parts if f])
    domains = _extract_domains(fqdn_str)
    port = _extract_port(fqdn_str)

    if fqdn_str and fqdn_str not in description:
        description = f"{description} | FQDN: {fqdn_str}".strip(" | ")

    custom_fields = {}
    if domains:
        custom_fields["domains"] = domains

    config_url = None
    env_id = svc.get("environment_id")
    svc_uuid = svc.get("uuid")
    
    coolify_uuids = []
    if svc_uuid: coolify_uuids.append(svc_uuid)
    for app in svc.get("applications", []):
        auuid = app.get("uuid")
        if auuid: coolify_uuids.append(auuid)
        
    if coolify_uuids:
        custom_fields["coolify_uuids"] = coolify_uuids
        
    if env_id is not None and env_id in env_to_proj:
        proj_uuid, env_uuid = env_to_proj[env_id]
        if proj_uuid and env_uuid and svc_uuid:
            config_url = f"{host_url}/project/{proj_uuid}/environment/{env_uuid}/service/{svc_uuid}"

    return [Host(
        name=name,
        host_type="vm",
        status=status,
        source="coolify",
        description=description or f"Coolify service: {name}",
        platform="docker",
        cluster_name="Coolify",
        custom_fields=custom_fields,
        config_url=config_url,
        port=port,
        netbox_sync_protected=svc.get("is_sync_protected", False),
    )]

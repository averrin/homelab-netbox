"""Merger — combines collector outputs into a single DesiredState.

Matching algorithm:
1. Primary hosts come from Proxmox and Coolify (they don't overlap).
2. Pulse hosts enrich existing hosts by name-first, then IP matching.
3. NPM services attach to hosts by domain or forward_host IP.
4. Services are no longer separate objects; they enrich host URL metadata.
"""

from __future__ import annotations

from urllib.parse import urlparse

from models import DesiredState, Host


def merge(
    proxmox_hosts: list[Host] | None = None,
    coolify_hosts: list[Host] | None = None,
    pulse_hosts: list[Host] | None = None,
    npm_services: list = None, # Services list from NPM collector
) -> DesiredState:
    """Merge all collector outputs into a unified DesiredState."""
    state = DesiredState()

    # Step 1: Add primary hosts (Proxmox + Coolify — non-overlapping)
    for host in (proxmox_hosts or []):
        key = host.name.lower()
        if key in state.hosts:
            vmid = getattr(host, "vmid", None)
            key = f"{key}-{vmid}" if vmid else f"{key}-2"
        state.hosts[key] = host

    for host in (coolify_hosts or []):
        state.hosts[host.name.lower()] = host

    # Build IP index after primary hosts
    state.build_ip_index()

    # Step 2: Merge Pulse hosts into existing hosts or add as new
    for pulse_host in (pulse_hosts or []):
        matched_key = _find_match(pulse_host, state)
        if matched_key:
            _enrich_host(state.hosts[matched_key], pulse_host)
        else:
            # Pulse host with no match — add as new
            state.hosts[pulse_host.name.lower()] = pulse_host

    # Rebuild index after Pulse additions
    state.build_ip_index()

    # Step 3: Absorb NPM services into hosts by domain or forward_host IP
    domain_index = _build_domain_index(state)
    for svc in (npm_services or []):
        # svc here is still the data object from NPM collector (which might look like the old Service model)
        # Domain match first
        matched_key = _match_service_by_domain(svc, domain_index)
        if matched_key:
            _enrich_host_from_npm(state.hosts[matched_key], svc)
            continue

        # Try IP match
        if hasattr(svc, "forward_host") and svc.forward_host and svc.forward_host in state.ip_index:
            host_key = state.ip_index[svc.forward_host]
            _enrich_host_from_npm(state.hosts[host_key], svc)

    # Step 4: Final Cleanup & Singular URL selection
    for host in state.hosts.values():
        # Clean up IPs - only 192.168.x.x
        for interface in host.interfaces:
            interface.ip_addresses = [
                ip for ip in interface.ip_addresses 
                if ip.address.startswith("192.168.")
            ]
        host.interfaces = [iface for iface in host.interfaces if iface.ip_addresses]

        # Consolidate URLs into singular fields
        # If Coolify/Pulse added plural lists in custom_fields, pick first
        _finalize_urls(host)

    # Rebuild index one last time after filtering
    state.build_ip_index()

    return state


def _find_match(pulse_host: Host, state: DesiredState) -> str | None:
    """Try to match a Pulse host to an existing host: name first, then IP."""
    # Name match
    key = pulse_host.name.lower()
    if key in state.hosts:
        return key

    # IP match — prefer 192.168.x.x
    preferred_ip = pulse_host.get_preferred_ip()
    if preferred_ip and preferred_ip in state.ip_index:
        return state.ip_index[preferred_ip]

    # Try all IPs
    for ip in pulse_host.get_all_ips():
        if ip in state.ip_index:
            return state.ip_index[ip]

    # Additional Match: Coolify UUIDs embedded in Pulse container names
    pulse_name = pulse_host.name.lower()
    for key, h in state.hosts.items():
        if h.source == "coolify" and "coolify_uuids" in h.custom_fields:
            for cuuid in h.custom_fields["coolify_uuids"]:
                if cuuid and cuuid.lower() in pulse_name:
                    return key

    return None


def _build_domain_index(state: DesiredState) -> dict[str, str]:
    """Build domain → host-key index from hosts with domain custom fields."""
    index: dict[str, str] = {}
    for key, host in state.hosts.items():
        # Check custom_fields["domains"] (list of strings)
        domains = host.custom_fields.get("domains")
        if isinstance(domains, list):
            for domain in domains:
                index[domain.lower()] = key
        elif isinstance(domains, str):
            index[domains.lower()] = key
    return index


def _match_service_by_domain(svc, domain_index: dict[str, str]) -> str | None:
    """Try to match an NPM service to a host by comparing its external URLs."""
    urls = getattr(svc, "external_urls", [])
    for url in urls:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if hostname and hostname.lower() in domain_index:
            return domain_index[hostname.lower()]
    return None


def _enrich_host_from_npm(host: Host, svc) -> None:
    """Absorb NPM proxy data into a host.
    
    Sets external_url if not set or if it's currently a placeholder.
    Adds forward IP to host interfaces.
    """
    from models import Interface, IPAddress

    # Add forward_host as a real IP
    forward_host = getattr(svc, "forward_host", None)
    if forward_host:
        existing_ips = set(host.get_all_ips())
        if forward_host not in existing_ips:
            ip = IPAddress(address=forward_host, source="npm")
            if host.interfaces:
                host.interfaces[0].ip_addresses.append(ip)
            else:
                host.interfaces.append(Interface(name="eth0", ip_addresses=[ip]))

    # Set singular URLs
    ext_urls = getattr(svc, "external_urls", [])
    if ext_urls:
        ext_urls.sort(key=lambda url: "averr.in" not in url)
        first_new = ext_urls[0]
        current = host.external_url or ""
        # If current is empty, or current is wildcard and new one is not
        if not current or ("*" in current and "*" not in first_new):
            host.external_url = first_new

    int_urls = getattr(svc, "internal_urls", [])
    if int_urls and not host.internal_url:
        host.internal_url = int_urls[0]


def _enrich_host(existing: Host, pulse_host: Host) -> None:
    """Enrich an existing host with data from Pulse."""
    if pulse_host.status == "active" and existing.status != "active":
        existing.status = pulse_host.status

    if pulse_host.description and pulse_host.description not in existing.description:
        sep = " | " if existing.description else ""
        existing.description += f"{sep}{pulse_host.description}"

    # Merge IPs
    existing_ips = set(existing.get_all_ips())
    for iface in pulse_host.interfaces:
        for ip in iface.ip_addresses:
            if ip.address not in existing_ips:
                if existing.interfaces:
                    existing.interfaces[0].ip_addresses.append(ip)
                else:
                    from models import Interface
                    existing.interfaces.append(Interface(name="eth0", ip_addresses=[ip]))
                existing_ips.add(ip.address)

    # Merge URL fields from Pulse if they exist
    if pulse_host.external_url and not existing.external_url:
        existing.external_url = pulse_host.external_url
    if pulse_host.internal_url and not existing.internal_url:
        existing.internal_url = pulse_host.internal_url


def _finalize_urls(host: Host) -> None:
    """Pulls URLs from custom_fields (plural) into singular fields if empty."""
    # Handle older/plural fields from various collectors
    # External
    if not host.external_url:
        ext = host.custom_fields.get("external_urls") or host.custom_fields.get("external_url")
        if isinstance(ext, list) and ext:
            host.external_url = ext[0]
        elif isinstance(ext, str):
            host.external_url = ext

    # Internal
    if not host.internal_url:
        int_u = host.custom_fields.get("internal_urls") or host.custom_fields.get("internal_url")
        if isinstance(int_u, list) and int_u:
            host.internal_url = int_u[0]
        elif isinstance(int_u, str):
            host.internal_url = int_u

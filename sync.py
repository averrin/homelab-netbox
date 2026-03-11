"""Sync pipeline — the core logic that collects, merges, reconciles, and executes.

This module contains the pipeline logic, separated from CLI argument parsing.
It can be called programmatically or from cli.py.
"""

from __future__ import annotations

import os
import sys

from config import Config
from merger import merge
from reconciler import get_nb_client, reconcile
import executor
from executor import execute

# Required environment variables per source
_SOURCE_VARS: dict[str, list[str]] = {
    "coolify": ["COOLIFY_URL", "COOLIFY_TOKEN"],
    "proxmox": ["PVE_API_HOST", "PVE_API_USER", "PVE_API_TOKEN", "PVE_API_SECRET"],
    "pulse": ["PULSE_URL", "PULSE_TOKEN"],
    "npm": ["NPM_URL", "NPM_EMAIL", "NPM_PASSWORD"],
}


def run_sync(
    cfg: Config,
    dry_run: bool = False,
    verbose: bool = False,
    sources: set[str] | list[str] | None = None,
    exporters: set[str] | list[str] | None = None,
) -> None:
    """Run the full synchronization pipeline."""
    # Phase 0: Check dependencies and environment
    active = []
    if sources:
        active = [s.lower() for s in sources]
    else:
        active = cfg.available_sources()
        
    exporters = exporters or []

    missing_vars = []
    for source in active:
        if source in _SOURCE_VARS:
            for var in _SOURCE_VARS[source]:
                if not os.environ.get(var):
                    missing_vars.append(var)

    if missing_vars:
        print(f"Error: Missing environment variables for active sources: {', '.join(missing_vars)}")
        sys.exit(1)

    print(f"Sources: {', '.join(active)}")

    # Phase 1: Collect
    from collectors.proxmox import collect as collect_proxmox
    from collectors.coolify import collect as collect_coolify
    from collectors.pulse import collect as collect_pulse
    from collectors.npm import collect as collect_npm
    from models import Host

    proxmox_hosts: list[Host] = []
    coolify_hosts: list[Host] = []
    pulse_hosts: list[Host] = []
    npm_services: list = []

    if "proxmox" in active:
        print("Collecting from Proxmox...")
        for pve_cfg in cfg.proxmox:
            try:
                hosts_from_pve = collect_proxmox(pve_cfg)
                proxmox_hosts.extend(hosts_from_pve)
                print(f"  → {pve_cfg.host}: {len(hosts_from_pve)} hosts (nodes + VMs + containers)")
            except Exception as e:
                host_label = getattr(pve_cfg, 'host', 'Unknown')
                print(f"  ERROR: {host_label}: {e}")

    if "coolify" in active:
        print("Collecting from Coolify...")
        try:
            coolify_hosts = collect_coolify(cfg.coolify)
            print(f"  → {len(coolify_hosts)} applications + services")
        except Exception as e:
            print(f"  ERROR: {e}")

    if "pulse" in active:
        print("Collecting from Pulse...")
        try:
            pulse_hosts = collect_pulse(cfg.pulse)
            print(f"  → {len(pulse_hosts)} containers")
        except Exception as e:
            print(f"  ERROR: {e}")

    if "npm" in active:
        print("Collecting from NPM...")
        try:
            npm_services = collect_npm(cfg.npm)
            print(f"  → {len(npm_services)} proxy services")
        except Exception as e:
            print(f"  ERROR: {e}")

    # Phase 2: Merge
    print("\nMerging desired state...")
    from models import DesiredState
    desired = merge(
        proxmox_hosts=proxmox_hosts,
        coolify_hosts=coolify_hosts,
        pulse_hosts=pulse_hosts,
        npm_services=npm_services,
    )
    print(f"  → {len(desired.hosts)} hosts")

    # Phase 3: Reconcile
    print("\nReconciling against NetBox...")
    nb = get_nb_client(cfg.netbox.url, cfg.netbox.token)
    
    # Ensure custom fields exist
    executor.ensure_custom_fields(nb)
    
    actions = reconcile(desired, nb)

    create_count = sum(1 for a in actions if a.verb == "create")
    update_count = sum(1 for a in actions if a.verb == "update")
    delete_count = sum(1 for a in actions if a.verb == "delete")
    skip_count = sum(1 for a in actions if a.verb == "skip")
    print(f"  → {len(actions)} actions ({create_count} create, {update_count} update, {delete_count} delete, {skip_count} skip)")

    # Phase 4: Execute
    print()
    execute(actions, nb, dry_run=dry_run, verbose=verbose)

    # Phase 5: Export (only if not dry-run)
    if not dry_run and "infisical" in exporters and cfg.infisical.is_configured:
        print("\nExporting to Infisical...")
        from exporters.infisical import export as export_infisical
        try:
            export_infisical(cfg.netbox.url, cfg.netbox.token, cfg.infisical)
            print("  → Export complete")
        except Exception as e:
            print(f"  ERROR: Infisical export failed: {e}")

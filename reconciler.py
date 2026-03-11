"""Reconciler — compares DesiredState with NetBox and produces Actions."""

from __future__ import annotations

import pynetbox

from models import Action, DesiredState, Host


def get_nb_client(url: str, token: str) -> pynetbox.api:
    """Initialize and return a pynetbox API client."""
    nb = pynetbox.api(url, token=token)
    nb.http_session.verify = False
    return nb


def reconcile(desired: DesiredState, nb: pynetbox.api) -> list[Action]:
    """Compare requested state with actual NetBox state and return a list of actions."""
    # 1. Load current NetBox state
    existing_hosts = _load_netbox_state(nb)

    actions: list[Action] = []

    # 2. Reconcile primary hosts (Devices and VMs)
    for name, host in desired.hosts.items():
        if name in existing_hosts:
            # Update existing
            existing = existing_hosts.pop(name)
            actions.append(_reconcile_host(host, existing, nb))
        else:
            # Create new
            actions.append(Action(verb="create", object_type=host.host_type, target=host.name, details=_host_to_details(host)))

    # 3. Deletion logic: Anything left in existing_hosts was NOT in desired state
    for name, existing in existing_hosts.items():
        # Check protection
        is_protected = _is_protected(existing)
        if is_protected:
            actions.append(Action(verb="skip", object_type=existing["type"], target=name, reason="Protected from deletion"))
        else:
            actions.append(Action(verb="delete", object_type=existing["type"], target=name, details={"id": existing["id"]}, reason="Not in desired state and not protected"))

    # 4. Global deletion of ALL services created by sync (since we don't need them anymore)
    # We find all services and mark them for deletion unless protected
    all_services = nb.ipam.services.all()
    for svc in all_services:
        is_protected = getattr(svc.custom_fields, "netbox_sync_protected", False)
        if is_protected:
            actions.append(Action(verb="skip", object_type="service", target=svc.name, reason="Protected service preserved"))
        else:
            # Find parent host name for logging
            host_name = "Unknown"
            device = getattr(svc, "device", None)
            vm = getattr(svc, "virtual_machine", None)
            
            if device: host_name = device.name
            elif vm: host_name = vm.name
            
            actions.append(Action(verb="delete", object_type="service", target=f"{host_name}/{svc.name}", details={"id": svc.id}, reason="Deprecated model layer"))

    return actions


def _load_netbox_state(nb: pynetbox.api) -> dict[str, dict]:
    """Fetch all relevant devices and VMs from NetBox for comparison."""
    state = {}

    # Fetch Devices
    for d in nb.dcim.devices.all():
        name = d.name.lower()
        state[name] = {
            "id": d.id,
            "type": "device",
            "obj": d,
            "status": d.status.value if hasattr(d.status, "value") else str(d.status),
            "description": d.description or "",
            "custom_fields": dict(d.custom_fields),
            "tags": [t.name for t in d.tags] if d.tags else [],
            "platform": d.platform.name if getattr(d, "platform", None) else None,
            "primary_ip": d.primary_ip4.address.split("/")[0] if d.primary_ip4 else None,
            "cluster": d.cluster.name if getattr(d, "cluster", None) else None,
        }

    # Fetch VMs
    for v in nb.virtualization.virtual_machines.all():
        name = v.name.lower()
        state[name] = {
            "id": v.id,
            "type": "vm",
            "obj": v,
            "status": v.status.value if hasattr(v.status, "value") else str(v.status),
            "description": v.description or "",
            "custom_fields": dict(v.custom_fields),
            "tags": [t.name for t in v.tags] if v.tags else [],
            "platform": v.platform.name if getattr(v, "platform", None) else None,
            "vcpus": v.vcpus,
            "memory": v.memory,
            "primary_ip": v.primary_ip4.address.split("/")[0] if v.primary_ip4 else None,
            "cluster": v.cluster.name if getattr(v, "cluster", None) else None,
        }

    return state


def _is_protected(existing: dict) -> bool:
    """Check if an existing NetBox object has the sync-protected flag set."""
    cfs = existing.get("custom_fields", {})
    return cfs.get("netbox_sync_protected") is True


def _reconcile_host(desired: Host, existing: dict, nb: pynetbox.api) -> Action:
    """Compare a single desired host with its NetBox counterpart."""
    diff = {}
    
    # 1. Base fields
    if desired.status.lower() != existing["status"].lower():
        diff["status"] = desired.status

    if desired.description:
        desired_desc = desired.description[:200]
        if desired_desc != existing["description"]:
            diff["description"] = desired_desc

    if desired.platform:
        existing_platform = existing.get("platform") or ""
        if desired.platform.lower() != existing_platform.lower():
            diff["platform"] = desired.platform

    # 1.5 Cluster
    existing_cluster = existing.get("cluster") or ""
    if desired.cluster_name and desired.cluster_name != existing_cluster:
        diff["cluster"] = desired.cluster_name

    # 2. Resource fields (VM only)
    if desired.host_type == "vm":
        if desired.vcpus and float(desired.vcpus) != float(existing.get("vcpus") or 0):
            diff["vcpus"] = desired.vcpus
        if desired.memory_mb and int(desired.memory_mb) != int(existing.get("memory") or 0):
            diff["memory"] = desired.memory_mb

    # 3. Custom Fields
    new_cfs = dict(existing["custom_fields"])
    cf_changed = False
    
    # Map new singular fields
    cf_map = {
        "config_url": desired.config_url,
        "external_url": desired.external_url,
        "internal_url": desired.internal_url,
        "vmid": desired.vmid,
    }
    
    for cf_key, cf_val in cf_map.items():
        val_to_apply = cf_val
        
        # MIGRATION: If desired is empty but NetBox has plural data, migrate it
        if val_to_apply is None or val_to_apply == "":
            legacy_key = f"{cf_key}s" # e.g. external_urls
            legacy_val = new_cfs.get(legacy_key)
            if legacy_val and isinstance(legacy_val, list) and legacy_val:
                val_to_apply = legacy_val[0]
            elif legacy_val and isinstance(legacy_val, str) and legacy_val:
                val_to_apply = legacy_val
        
        # If we have a value and it differs from what's currently in the singular field
        current_val = str(new_cfs.get(cf_key, "")).strip()
        if val_to_apply and str(val_to_apply).strip() != current_val:
            new_cfs[cf_key] = val_to_apply
            cf_changed = True

    if cf_changed:
        diff["custom_fields"] = new_cfs

    # 4. Primary IP
    desired_ip = desired.get_preferred_ip()
    if desired_ip and desired_ip != existing.get("primary_ip"):
        diff["primary_ip4"] = desired_ip

    # 5. Tags
    if desired.tags:
        desired_tags = sorted([t.lower() for t in desired.tags])
        existing_tags = sorted([t.lower() for t in existing["tags"]])
        if desired_tags != existing_tags:
            diff["tags"] = desired.tags

    if diff:
        diff["id"] = existing["id"]
        return Action(verb="update", object_type=desired.host_type, target=desired.name, details=diff)
    
    return Action(verb="skip", object_type=desired.host_type, target=desired.name, reason="Matches NetBox state")


def _host_to_details(host: Host) -> dict:
    """Convert a Host model to a dictionary of creation details."""
    details = {
        "name": host.name,
        "status": host.status,
        "description": host.description[:200] if host.description else "",
        "custom_fields": {
            "vmid": host.vmid,
            "config_url": host.config_url,
            "external_url": host.external_url,
            "internal_url": host.internal_url,
            "netbox_sync_protected": host.netbox_sync_protected,
        },
        "tags": host.tags,
    }
    
    if host.platform:
        details["platform"] = host.platform
    
    preferred_ip = host.get_preferred_ip()
    if preferred_ip:
        details["primary_ip4"] = preferred_ip

    if host.host_type == "vm":
        details["vcpus"] = host.vcpus
        details["memory"] = host.memory_mb
        
    if host.cluster_name:
        details["cluster"] = host.cluster_name
    
    # Merge any other custom fields
    details["custom_fields"].update(host.custom_fields)
    
    return details

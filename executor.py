"""Executor — applies Actions to NetBox or prints them in dry-run mode.

Two modes:
- dry_run=True: prints formatted diff output, changes nothing
- dry_run=False: applies each Action to NetBox via pynetbox
"""

from __future__ import annotations

import sys
import ipaddress as ipaddr_mod

import pynetbox

from models import Action


# ANSI colors for terminal output
_COLORS = {
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
    "reset": "\033[0m",
    "bold": "\033[1m",
}


def _supports_color() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(name: str) -> str:
    return _COLORS.get(name, "") if _supports_color() else ""


def execute(
    actions: list[Action],
    nb: pynetbox.api,
    dry_run: bool = False,
    verbose: bool = False,
) -> None:
    """Execute or print the list of actions."""
    stats = {"create": 0, "update": 0, "skip": 0, "delete": 0}

    for action in actions:
        stats[action.verb] = stats.get(action.verb, 0) + 1

        if action.verb == "skip" and not verbose:
            continue

        if dry_run:
            _print_action(action)
        else:
            success = _apply_action(action, nb)
            if success:
                _print_action(action, prefix="APPLIED")

    # Summary
    mode = f"{_c('bold')}DRY RUN{_c('reset')}" if dry_run else f"{_c('bold')}SYNC{_c('reset')}"
    print(
        f"\n{mode} complete: "
        f"{_c('green')}{stats.get('create', 0)} create{_c('reset')}, "
        f"{_c('yellow')}{stats.get('update', 0)} update{_c('reset')}, "
        f"{_c('dim')}{stats.get('skip', 0)} skip{_c('reset')}, "
        f"{_c('yellow')}{stats.get('delete', 0)} delete{_c('reset')}"
    )


def _print_action(action: Action, prefix: str | None = None) -> None:
    """Pretty-print a single action."""
    tag = prefix or ("DRY RUN" if action.verb != "skip" else "SKIP")

    verb_color = {
        "create": _c("green"),
        "update": _c("yellow"),
        "delete": _c("yellow"),
        "skip": _c("dim"),
    }.get(action.verb, "")

    header = (
        f"[{tag}] "
        f"{verb_color}{action.verb.upper()}{_c('reset')} "
        f"{action.object_type} "
        f"{_c('cyan')}\"{action.target}\"{_c('reset')}"
    )

    if action.reason:
        header += f" — {action.reason}"

    print(header)

    if action.details and action.verb != "skip":
        for key, value in action.details.items():
            if isinstance(value, dict) and "old" in value and "new" in value:
                print(f"  {key}: {value['old']} → {value['new']}")
            else:
                print(f"  {key}: {value}")


def ensure_custom_fields(nb: pynetbox.api) -> None:
    """Ensure required custom fields exist in NetBox.
    
    Handles both NetBox 3.x (content_types) and 4.x (object_types).
    """
    fields = [
        # name, label, type, ctypes
        ("vmid", "VM ID", "integer", ["virtualization.virtualmachine"]),
        ("config_url", "Config URL", "url", ["virtualization.virtualmachine", "dcim.device"]),
        ("external_url", "External URL", "url", ["virtualization.virtualmachine", "dcim.device"]),
        ("internal_url", "Internal URL", "url", ["virtualization.virtualmachine", "dcim.device"]),
        ("port", "Port", "integer", ["virtualization.virtualmachine", "dcim.device"]),
        ("netbox_sync_protected", "Sync Protected", "boolean", ["virtualization.virtualmachine", "dcim.device"]),
        
        # Keep plural ones as legacy/deprecated but don't delete yet
        ("external_urls", "External URLs (Legacy)", "json", ["virtualization.virtualmachine", "dcim.device"]),
        ("internal_urls", "Internal URLs (Legacy)", "json", ["virtualization.virtualmachine", "dcim.device"]),
        
        # External tracking
        ("infisical_url", "Infisical Managed Secrets", "url", ["virtualization.virtualmachine", "dcim.device"]),

        # Monitoring hints
        ("skip_ssl_verify", "Skip SSL Verify", "boolean", ["virtualization.virtualmachine", "dcim.device"]),
        ("auth_type", "Auth Type", "text", ["virtualization.virtualmachine", "dcim.device"]),
        ("monitors", "Monitors", "text", ["virtualization.virtualmachine", "dcim.device"]),
    ]
    
    # Determine NetBox version (4.0+ uses core app and renames fields)
    is_nb4 = hasattr(nb, "core")
    
    existing_cfs = {cf.name: cf for cf in nb.extras.custom_fields.all()}

    for name, label, ftype, ctypes in fields:
        if name in existing_cfs:
            cf = existing_cfs[name]
            if getattr(cf, "required", False):
                try:
                    cf.required = False
                    cf.save()
                except Exception:
                    pass
            continue
            
        print(f"  {_c('dim')}Creating custom field '{label}' ({name})...{_c('reset')}")
        
        # Map types
        nb_type = {
            "integer": "integer",
            "text": "text",
            "url": "url",
            "json": "json",
            "boolean": "boolean",
        }.get(ftype, "text")
        
        try:
            create_kwargs = {
                "name": name,
                "label": label,
                "type": nb_type,
                "required": False,
                "default": False if ftype == "boolean" else None
            }
            
            if is_nb4:
                # NetBox 4.x requires string format ["app.model", ...]
                create_kwargs["object_types"] = ctypes
            else:
                # NetBox 3.x resolve IDs
                resolved_ids = []
                for ct_str in ctypes:
                    app, mod = ct_str.split(".")
                    obj = nb.extras.content_types.get(app_label=app, model=mod)
                    if obj:
                        resolved_ids.append(obj.id)
                create_kwargs["content_types"] = resolved_ids
            
            nb.extras.custom_fields.create(**create_kwargs)
        except Exception as e:
            print(f"  {_c('yellow')}Error creating custom field {name}: {e}{_c('reset')}")


def _apply_action(action: Action, nb: pynetbox.api) -> bool:
    """Apply a single action to NetBox. Skips are no-ops. Returns True on success."""
    if action.verb == "skip":
        return True

    try:
        if action.verb == "delete":
            return _apply_delete(action, nb)

        if action.object_type == "device":
            _apply_device(action, nb)
        elif action.object_type in ("vm", "container"):
            _apply_vm(action, nb)
        elif action.object_type == "ip":
            _apply_ip(action, nb)
            
        return True
    except Exception as e:
        print(f"  {_c('bold')}ERROR{_c('reset')} applying {action.verb} {action.object_type} \"{action.target}\": {e}")
        return False


def _apply_delete(action: Action, nb: pynetbox.api) -> bool:
    """Delete an object from NetBox."""
    obj_id = action.details.get("id")
    if not obj_id:
        return False

    try:
        if action.object_type == "device":
            obj = nb.dcim.devices.get(obj_id)
        elif action.object_type in ("vm", "container"):
            obj = nb.virtualization.virtual_machines.get(obj_id)
        elif action.object_type == "service":
            obj = nb.ipam.services.get(obj_id)
        else:
            return False

        if obj:
            obj.delete()
            return True
        return False
    except Exception as e:
        print(f"  {_c('bold')}ERROR{_c('reset')} deleting {action.object_type} \"{action.target}\": {e}")
        return False


def _apply_device(action: Action, nb: pynetbox.api) -> None:
    if action.verb == "create":
        site = next(iter(nb.dcim.sites.all()), None)
        if not site:
            print("  ERROR: No sites in NetBox. Create a site first.")
            return

        role_name = action.details.get("role", "Server")
        role = nb.dcim.device_roles.get(name=role_name)
        if not role:
            role = nb.dcim.device_roles.create(
                name=role_name, slug=role_name.lower().replace(" ", "-"), color="2196f3"
            )

        device_type = next(iter(nb.dcim.device_types.all()), None)
        if not device_type:
            mfr = next(iter(nb.dcim.manufacturers.all()), None)
            if not mfr:
                mfr = nb.dcim.manufacturers.create(name="Generic", slug="generic")
            device_type = nb.dcim.device_types.create(
                manufacturer=mfr.id, model="Generic Server", slug="generic-server"
            )

        create_kwargs = {
            "name": action.details["name"],
            "device_type": device_type.id,
            "role": role.id,
            "site": site.id,
            "description": action.details.get("description", ""),
            "status": action.details.get("status", "active"),
            "custom_fields": action.details.get("custom_fields", {}),
        }
        
        cluster_name = action.details.get("cluster")
        if cluster_name:
            platform_str = action.details.get("platform", "")
            create_kwargs["cluster"] = _get_or_create_cluster(nb, cluster_name, is_device=True, platform_str=platform_str)
            
        if "platform" in action.details and action.details["platform"]:
            p = action.details["platform"]
            slug = p.lower().replace(" ", "-").replace(".", "-")
            platform = nb.dcim.platforms.get(slug=slug)
            if not platform:
                platform = nb.dcim.platforms.create(name=p, slug=slug)
            create_kwargs["platform"] = platform.id

        device = nb.dcim.devices.create(**create_kwargs)
        
        if "primary_ip4" in action.details:
            _set_primary_ip(device, action.details["primary_ip4"], nb)

    elif action.verb == "update":
        obj_id = action.details.get("id")
        obj = nb.dcim.devices.get(obj_id) if obj_id else nb.dcim.devices.get(name=action.target)
        if obj:
            _update_obj(obj, action.details, nb)


def _apply_vm(action: Action, nb: pynetbox.api) -> None:
    if action.verb == "create":
        cluster_name = action.details.get("cluster") or "Default"
        platform_str = action.details.get("platform", "")
        
        create_kwargs = {
            "name": action.details["name"],
            "cluster": _get_or_create_cluster(nb, cluster_name, is_device=False, platform_str=platform_str),
            "status": action.details.get("status", "active"),
            "custom_fields": action.details.get("custom_fields", {}),
        }
        if action.details.get("vcpus"):
            create_kwargs["vcpus"] = action.details["vcpus"]
        if action.details.get("memory"):
            create_kwargs["memory"] = action.details["memory"]

        if action.details.get("tags"):
            tags = []
            for t_name in action.details["tags"]:
                t = nb.extras.tags.get(name=t_name)
                if not t:
                    t = nb.extras.tags.create(name=t_name, slug=t_name.lower().replace(" ", "-").replace(".", "-"))
                tags.append({"id": t.id})
            create_kwargs["tags"] = tags
            
        if "platform" in action.details and action.details["platform"]:
            p = action.details["platform"]
            slug = p.lower().replace(" ", "-").replace(".", "-")
            platform = nb.dcim.platforms.get(slug=slug)
            if not platform:
                platform = nb.dcim.platforms.create(name=p, slug=slug)
            create_kwargs["platform"] = platform.id

        vm = nb.virtualization.virtual_machines.create(**create_kwargs)
        
        if "primary_ip4" in action.details:
            _set_primary_ip(vm, action.details["primary_ip4"], nb)

    elif action.verb == "update":
        obj_id = action.details.get("id")
        obj = nb.virtualization.virtual_machines.get(obj_id) if obj_id else nb.virtualization.virtual_machines.get(name=action.target)
        if obj:
            _update_obj(obj, action.details, nb)


def _update_obj(obj, details: dict, nb: pynetbox.api) -> None:
    """Consolidated update logic for Devices and VMs."""
    # 1. First update everything EXCEPT primary_ip4 to ensure metadata is saved even if IP fails
    for key, val in details.items():
        if key in ("primary_ip4", "id"):
            continue
            
        if key == "tags" and isinstance(val, list):
            updated_tags = []
            for t_name in val:
                t = nb.extras.tags.get(name=t_name)
                if not t:
                    t = nb.extras.tags.create(name=t_name, slug=t_name.lower().replace(" ", "-").replace(".", "-"))
                updated_tags.append({"id": t.id})
            setattr(obj, "tags", updated_tags)
        elif key == "platform":
            if val:
                slug = val.lower().replace(" ", "-").replace(".", "-")
                platform = nb.dcim.platforms.get(slug=slug)
                if not platform:
                    platform = nb.dcim.platforms.create(name=val, slug=slug)
                setattr(obj, "platform", platform.id)
            else:
                setattr(obj, "platform", None)
        elif key == "cluster":
            if val:
                is_device = hasattr(obj, "device_type")
                p_str = details.get("platform") or getattr(getattr(obj, "platform", None), "name", "")
                cluster_id = _get_or_create_cluster(nb, val, is_device=is_device, platform_str=p_str)
                setattr(obj, "cluster", cluster_id)
            else:
                setattr(obj, "cluster", None)
        else:
            setattr(obj, key, val)
    
    obj.save()
    
    # 2. Then attempt primary_ip4 update
    if "primary_ip4" in details:
        try:
            _set_primary_ip(obj, details["primary_ip4"], nb)
            obj.save()
        except Exception as e:
             print(f"  {_c('yellow')}Warning: Could not set primary_ip4 for {obj.name}: {e}{_c('reset')}")


def _set_primary_ip(obj, ip_address: str, nb: pynetbox.api) -> None:
    """Find the IP object and set it as primary_ip4 for the given Device/VM."""
    # Note: ip_address can be just the IP or CIDR. NetBox API works well with either.
    ip_obj = nb.ipam.ip_addresses.get(address=ip_address)
    if not ip_obj:
        # Fallback: maybe it's in NetBox without CIDR or with a different one
        ip_obj = nb.ipam.ip_addresses.get(address=ip_address.split("/")[0])
        
    if ip_obj:
        is_device = hasattr(obj, "device_type")
        
        # Ensure the IP is assigned to an interface on this object first
        if is_device:
            ifaces = list(nb.dcim.interfaces.filter(device_id=obj.id))
        else:
            ifaces = list(nb.virtualization.interfaces.filter(virtual_machine_id=obj.id))
            
        iface = next((i for i in ifaces if i.name == "eth0"), None)
        if not iface:
            if is_device:
                iface = nb.dcim.interfaces.create(device=obj.id, name="eth0", type="1000base-t")
            else:
                iface = nb.virtualization.interfaces.create(virtual_machine=obj.id, name="eth0")
                
        # Assign IP to interface if unassigned or assigned to a different object
        if not getattr(ip_obj, "assigned_object_id", None) or ip_obj.assigned_object_id != iface.id:
            try:
                ip_obj.assigned_object_type = "dcim.interface" if is_device else "virtualization.vminterface"
                ip_obj.assigned_object_id = iface.id
                ip_obj.save()
            except Exception as e:
                print(f"  {_c('yellow')}Warning: Could not assign IP '{ip_address}' to {obj.name}'s interface: {e}{_c('reset')}")
                return
            
        # pynetbox allows setting by ID or object
        try:
            obj.primary_ip4 = ip_obj.id
            obj.save()
        except Exception as e:
            print(f"  {_c('yellow')}Warning: Could not set primary_ip4 on {obj.name}: {e}{_c('reset')}")


def _apply_ip(action: Action, nb: pynetbox.api) -> None:
    if action.verb != "create":
        return
    address = action.details.get("address")
    if not address:
        return

    try:
        ipaddr_mod.ip_interface(address)
    except ValueError:
        return

    try:
        nb.ipam.ip_addresses.create(address=address, status="active")
    except Exception:
        pass


def _get_or_create_cluster(nb: pynetbox.api, cluster_name: str, is_device: bool, platform_str: str) -> int | None:
    """Resolve a cluster ID by name, creating the cluster and cluster_type if missing."""
    if not cluster_name:
        return None

    cluster = nb.virtualization.clusters.get(name=cluster_name)
    if cluster:
        return cluster.id

    # Case-insensitive fallback: "coolify" → "Coolify"
    candidates = list(nb.virtualization.clusters.filter(name__ic=cluster_name))
    match = next((c for c in candidates if c.name.lower() == cluster_name.lower()), None)
    if match:
        return match.id
        
    if is_device:
        ct_name = "Proxmox"
    else:
        p_str = (platform_str or "").lower()
        if p_str in ("proxmox", "qemu", "lxc"):
            ct_name = "Proxmox"
        else:
            ct_name = "Docker/Container"

    ct = nb.virtualization.cluster_types.get(name=ct_name)
    if not ct:
        ct = nb.virtualization.cluster_types.create(
            name=ct_name, slug=ct_name.lower().replace("/", "-").replace(" ", "-")
        )
    cluster = nb.virtualization.clusters.create(name=cluster_name, type=ct.id)
    return cluster.id

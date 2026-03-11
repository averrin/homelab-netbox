"""Infisical exporter — syncs NetBox server data to Infisical secrets."""

from __future__ import annotations

from config import InfisicalConfig
from reconciler import get_nb_client


def export(netbox_url: str, netbox_token: str, cfg: InfisicalConfig) -> None:
    """Read servers (Devices + VMs) from NetBox and push their info to Infisical."""
    if not cfg.is_configured:
        print("Infisical not configured — skipping export.")
        return

    try:
        from infisical_sdk import InfisicalSDKClient
    except ImportError:
        print("infisical_sdk not installed — skipping export.")
        return

    nb = get_nb_client(netbox_url, netbox_token)

    client = InfisicalSDKClient(host=cfg.url.rstrip("/"))
    client.auth.universal_auth.login(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
    )

    # 1. Fetch all 'active' hosts that are Servers (role name match)
    # We check both DCIM devices and Virtual Machines
    hosts = []
    
    # Devices
    role = nb.dcim.device_roles.get(name="Server")
    if role:
        hosts.extend(nb.dcim.devices.filter(role_id=role.id, status="active"))
    
    # VMs (we treat all VMs as servers for secret storage purpose)
    hosts.extend(nb.virtualization.virtual_machines.filter(status="active"))

    for host in hosts:
        # NetBox Pynetbox custom_fields return dictionaries, not Attribute objects
        try:
            internal_url = host.custom_fields.get("internal_url", "")
        except AttributeError:
             internal_url = getattr(host.custom_fields, "internal_url", "")
             
        if not internal_url:
            continue

        ip = None
        # Devices use primary_ip4, VMs use primary_ip
        primary_ip = getattr(host, "primary_ip4", getattr(host, "primary_ip", None))
        if primary_ip:
            ip = primary_ip.address.split("/")[0]

        try:
            external_url = host.custom_fields.get("external_url", "")
        except AttributeError:
             external_url = getattr(host.custom_fields, "external_url", "")
        
        # Check NetBox SOT for port first.
        try:
            port = host.custom_fields.get("port")
        except AttributeError:
             port = getattr(host.custom_fields, "port", None)
             
        if not port:
            port = "N/A"
            try:
                if ":" in internal_url.replace("http://", "").replace("https://", ""):
                    parts = internal_url.split(":")
                    port = parts[-1].split("/")[0]
            except Exception:
                pass

        folder_name = host.name
        folder_path = "/vms/"

        try:
            # Need to ensure the parent /vms folder exists first, then the host folder
            try:
                client.folders.create_folder(
                    name="vms",
                    environment_slug=cfg.environment,
                    project_id=cfg.project_id,
                    path="/",
                )
            except Exception:
                pass
                
            client.folders.create_folder(
                name=folder_name,
                environment_slug=cfg.environment,
                project_id=cfg.project_id,
                path=folder_path,
            )
        except Exception:
            pass  # folder may already exist

        secret_path = f"{folder_path}{folder_name}"
        secrets = {
            "IP": ip or "N/A",
            "PORT": port,
            "INTERNAL_URL": internal_url,
            "EXTERNAL_URL": external_url or "N/A",
        }

        for key, value in secrets.items():
            try:
                client.secrets.create_secret_by_name(
                    secret_name=key,
                    secret_value=str(value),
                    secret_path=secret_path,
                    environment_slug=cfg.environment,
                    project_id=cfg.project_id,
                    secret_comment=f"Synced from NetBox infrastructure host {host.name}",
                )
            except Exception:
                try:
                    client.secrets.update_secret_by_name(
                        current_secret_name=key,
                        secret_value=str(value),
                        secret_path=secret_path,
                        environment_slug=cfg.environment,
                        project_id=cfg.project_id,
                        secret_comment=f"Synced from NetBox infrastructure host {host.name}",
                    )
                except Exception as e:
                    print(f"Failed to sync secret {key} for {host.name}: {e}")

        # Construct infisical dashboard link
        base_url = cfg.url.rstrip('/')
        workspace_id = cfg.project_id
        
        dashboard_url = f"{base_url}/organizations/{cfg.org_id}/projects/{cfg.project_slug}/{workspace_id}/secrets/{cfg.environment}?secretPath=%2Fvms%2F{host.name}"
        
        try:
            current_url = host.custom_fields.get("infisical_url", "")
        except AttributeError:
            current_url = getattr(host.custom_fields, "infisical_url", "")
            
        if current_url != dashboard_url:
            # Pynetbox needs a brand new dictionary instance to detect changes reliably
            new_cfs = dict(getattr(host, "custom_fields", {}))
            new_cfs["infisical_url"] = dashboard_url
            try:
                host.update({"custom_fields": new_cfs})
                print(f"Exported {host.name} to Infisical & linked NetBox.")
            except Exception as e:
                print(f"Failed to save NetBox infisical_url for {host.name}: {e}")
        else:
            print(f"Synced {host.name} secrets to Infisical.")

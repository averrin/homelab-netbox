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
        ip = None
        # Devices use primary_ip4, VMs use primary_ip
        primary_ip = getattr(host, "primary_ip4", getattr(host, "primary_ip", None))
        if primary_ip:
            ip = primary_ip.address.split("/")[0]

        # Use new singular URL fields
        external_url = getattr(host.custom_fields, "external_url", "")
        internal_url = getattr(host.custom_fields, "internal_url", "")
        config_url = getattr(host.custom_fields, "config_url", "")

        folder_name = host.name
        folder_path = "/"

        try:
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
            "INTERNAL_LINK": internal_url or "N/A",
            "EXTERNAL_LINK": external_url or "N/A",
            "CONFIG_LINK": config_url or "N/A",
            "NETBOX_URL": f"{nb.base_url}/dcim/devices/{host.id}/" if hasattr(host, "device_type") else f"{nb.base_url}/virtualization/virtual-machines/{host.id}/",
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

        # Update comments with Infisical reference
        ref = f"\nInfisical Secrets Path: {secret_path} (Environment: {cfg.environment})"
        comments = getattr(host, "comments", "") or ""
        if "Infisical Secrets Path:" not in comments:
            host.comments = comments + ref
            host.save()
            print(f"Exported {host.name} to Infisical.")
        else:
            print(f"Synced {host.name} secrets to Infisical.")

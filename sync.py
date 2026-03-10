import os
import logging
import requests
import pynetbox
from dotenv import load_dotenv
from infisical_sdk import InfisicalSDKClient

logger = logging.getLogger(__name__)

INFISICAL_DEFAULT_URL = "https://app.infisical.com"
INFISICAL_DEFAULT_ENVIRONMENT = "prod"
INFISICAL_DEFAULT_SECRET_PATH = "/"

def load_config():
    """Load secrets from Infisical into os.environ, falling back to .env."""
    load_dotenv()

    client_id = os.environ.get("INFISICAL_CLIENT_ID")
    client_secret = os.environ.get("INFISICAL_CLIENT_SECRET")

    if not client_id or not client_secret:
        print("Infisical credentials not fully provided — relying on .env or existing environment variables.")
        return

    project_id = os.environ.get("INFISICAL_PROJECT_ID")
    if not project_id:
        print("INFISICAL_PROJECT_ID is required to fetch configuration secrets.")
        return

    environment = os.environ.get("INFISICAL_ENVIRONMENT", INFISICAL_DEFAULT_ENVIRONMENT)
    secret_path = os.environ.get("INFISICAL_SECRET_PATH", INFISICAL_DEFAULT_SECRET_PATH)
    site_url = os.environ.get("INFISICAL_URL", INFISICAL_DEFAULT_URL).rstrip("/")

    try:
        client = InfisicalSDKClient(host=site_url)
        client.auth.universal_auth.login(
            client_id=client_id,
            client_secret=client_secret,
        )
        response = client.secrets.list_secrets(
            environment_slug=environment,
            project_id=project_id,
            secret_path=secret_path,
        )

        injected = 0
        for s in response.secrets:
            if s.secretKey and s.secretKey not in os.environ:
                os.environ[s.secretKey] = s.secretValue
                injected += 1

        print(f"Loaded {injected} secrets from Infisical (env={environment}, path={secret_path})")
    except Exception as e:
        print(f"Failed to fetch configuration secrets from Infisical: {e}")

def get_coolify_servers(coolify_url, coolify_token):
    headers = {"Authorization": f"Bearer {coolify_token}"}
    url = f"{coolify_url}/api/v1/servers"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def get_pulse_containers(pulse_url, pulse_token):
    headers = {"X-API-Token": pulse_token}
    url = f"{pulse_url}/api/resources"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    resources = response.json()

    # Filter for container types. Pulse API might return a dict or list. Let's assume list of dicts.
    containers = []
    if isinstance(resources, list):
        for res in resources:
            res_type = res.get('type') or res.get('resourceType')
            if res_type in ['container', 'dockerContainer', 'docker']:
                containers.append(res)
    elif isinstance(resources, dict) and 'data' in resources:
        for res in resources['data']:
            res_type = res.get('type') or res.get('resourceType')
            if res_type in ['container', 'dockerContainer', 'docker']:
                containers.append(res)
    else:
        # If it's a flat dictionary of objects
        for k, res in resources.items():
            if isinstance(res, dict):
                res_type = res.get('type') or res.get('resourceType')
                if res_type in ['container', 'dockerContainer', 'docker']:
                    containers.append(res)
    return containers

def get_npm_proxy_hosts(npm_url, npm_token):
    headers = {"Authorization": f"Bearer {npm_token}"}
    url = f"{npm_url}/api/nginx/proxy-hosts"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

def sync_npm_to_netbox(proxies, nb):
    for proxy in proxies:
        domain_names = proxy.get('domain_names', [])
        forward_ip = proxy.get('forward_host')
        forward_port = proxy.get('forward_port')

        if not forward_ip or not domain_names:
            continue

        # Match by IP
        netbox_ip = forward_ip if '/' in forward_ip else f"{forward_ip}/32"

        try:
            ip_obj = nb.ipam.ip_addresses.get(address=netbox_ip)
        except Exception as e:
            print(f"Error finding IP {forward_ip} in NetBox: {e}. Skipping proxy for {domain_names}.")
            continue

        if not ip_obj:
            print(f"IP {forward_ip} not found in NetBox. Skipping proxy for {domain_names}.")
            continue

        device_id = None
        if ip_obj.assigned_object and hasattr(ip_obj.assigned_object, 'device'):
            device_id = ip_obj.assigned_object.device.id

        if not device_id:
            print(f"IP {forward_ip} is not assigned to a device. Skipping proxy for {domain_names}.")
            continue

        # Create service
        main_domain = domain_names[0]
        service_name = f"NPM Proxy - {main_domain}"
        service = nb.ipam.services.get(name=service_name, device_id=device_id)

        protocol = proxy.get('forward_scheme', 'http')
        protocol_val = "tcp" # Netbox requires tcp/udp/sctp, not http/https

        # We can add custom fields or simply put domain names in the description for external links
        description = f"External Domains: {', '.join(domain_names)} -> Internal: {protocol}://{forward_ip}:{forward_port}"

        if not service:
            print(f"Creating service {service_name} for device ID {device_id}...")
            service = nb.ipam.services.create(
                device=device_id,
                name=service_name,
                protocol=protocol_val,
                ports=[forward_port],
                description=description
            )
        else:
            print(f"Service {service_name} already exists. Updating...")
            service.description = description
            service.ports = [forward_port]
            service.save()

def sync_pulse_containers_to_netbox(containers, nb):
    try:
        cluster_type = nb.virtualization.cluster_types.get(name="Docker/Container")
        if not cluster_type:
            cluster_type = nb.virtualization.cluster_types.create(name="Docker/Container", slug="docker-container")

        cluster = nb.virtualization.clusters.get(name="Pulse Containers")
        if not cluster:
            cluster = nb.virtualization.clusters.create(name="Pulse Containers", type=cluster_type.id)

    except Exception as e:
        print(f"Error ensuring NetBox Cluster/ClusterType for Pulse: {e}")
        return

    for container in containers:
        # Pulse resource structure includes `id`, `name`, `status`, `ipAddresses`, `resourceType`
        name = container.get('name') or container.get('id')
        if not name:
            continue

        status = "active" if container.get('status', '').lower() == "running" else "offline"
        description = f"Pulse Container ID: {container.get('id')}"

        # Get existing VM
        vm = nb.virtualization.virtual_machines.get(name=name, cluster_id=cluster.id)
        if vm:
            print(f"Pulse container VM {name} already exists. Updating...")
            if vm.description != description or vm.status.value != status:
                vm.description = description
                vm.status = status
                vm.save()
        else:
            print(f"Creating Pulse container VM {name}...")
            vm = nb.virtualization.virtual_machines.create(
                name=name,
                cluster=cluster.id,
                description=description,
                status=status
            )

        # Handle IP Addresses
        ips = container.get('ipAddresses', [])
        # Sometimes IP is just a string
        if isinstance(ips, str):
            ips = [ips]

        primary_ip = None
        for ip_address in ips:
            if not ip_address:
                continue

            netbox_ip = ip_address if '/' in ip_address else f"{ip_address}/32"

            # Create/get interface
            interface_name = "eth0"
            interface = nb.virtualization.interfaces.get(virtual_machine_id=vm.id, name=interface_name)
            if not interface:
                interface = nb.virtualization.interfaces.create(
                    virtual_machine=vm.id,
                    name=interface_name
                )

            try:
                ip_obj = nb.ipam.ip_addresses.get(address=netbox_ip)
            except Exception as e:
                print(f"Error finding IP {ip_address} in NetBox: {e}")
                continue

            if not ip_obj:
                ip_obj = nb.ipam.ip_addresses.create(
                    address=netbox_ip,
                    status="active"
                )

            if not ip_obj.assigned_object_id or ip_obj.assigned_object_type != "virtualization.vminterface" or ip_obj.assigned_object_id != interface.id:
                ip_obj.assigned_object_type = "virtualization.vminterface"
                ip_obj.assigned_object_id = interface.id
                ip_obj.save()

            if not primary_ip:
                primary_ip = ip_obj

        if primary_ip and getattr(vm, 'primary_ip4', None) != primary_ip:
            vm.primary_ip4 = primary_ip.id
            vm.save()


def sync_netbox_to_infisical(nb, infisical_client, project_id, environment_slug):
    try:
        role = nb.dcim.device_roles.get(name="Server")
        if not role:
            print("No 'Server' role found in NetBox.")
            return
        servers = nb.dcim.devices.filter(role_id=role.id)
    except Exception as e:
        print(f"Error fetching servers from NetBox: {e}")
        return

    for server in servers:
        # Determine internal link, external link, and port based on primary IP and services
        ip = None
        if server.primary_ip4:
            ip = server.primary_ip4.address.split('/')[0]

        ports = []
        external_links = []
        internal_links = []

        if ip:
            services = nb.ipam.services.filter(device_id=server.id)
            for service in services:
                for port in service.ports:
                    ports.append(str(port))
                    internal_links.append(f"{service.protocol}://{ip}:{port}")

                # We placed external domains in the description in a previous step
                if "External Domains: " in service.description:
                    domains = service.description.split("External Domains: ")[1].split(" -> Internal:")[0]
                    for domain in domains.split(", "):
                        external_links.append(f"https://{domain.strip()}")

        folder_name = server.name
        folder_path = "/"

        # Create/Get folder in Infisical
        try:
            infisical_client.folders.create_folder(
                name=folder_name,
                environment_slug=environment_slug,
                project_id=project_id,
                path=folder_path
            )
        except Exception as e:
            # Folder might already exist, infisical throws an error but it's okay
            pass

        secret_path = f"{folder_path}{folder_name}"

        # Sync secrets
        secrets_to_sync = {
            "IP": ip or "N/A",
            "PORT": ",".join(ports) or "N/A",
            "INTERNAL_LINK": ",".join(internal_links) or "N/A",
            "EXTERNAL_LINK": ",".join(external_links) or "N/A",
            "NETBOX_URL": f"{nb.base_url}/dcim/devices/{server.id}/"
        }

        for key, value in secrets_to_sync.items():
            try:
                # Try creating
                infisical_client.secrets.create_secret_by_name(
                    secret_name=key,
                    secret_value=value,
                    secret_path=secret_path,
                    environment_slug=environment_slug,
                    project_id=project_id,
                    secret_comment=f"Synced from NetBox Server {server.name}"
                )
            except Exception as e:
                # If creating fails, try updating (e.g., secret already exists)
                # infisicalsdk throws error if secret exists
                try:
                    infisical_client.secrets.update_secret_by_name(
                        secret_name=key,
                        secret_value=value,
                        secret_path=secret_path,
                        environment_slug=environment_slug,
                        project_id=project_id
                    )
                except Exception as update_e:
                    print(f"Failed to sync secret {key} for server {server.name}: Create Error: {e}, Update Error: {update_e}")

        # Update NetBox server description to reference the Infisical folder
        infisical_reference = f"\nInfisical Secrets Path: {secret_path} (Environment: {environment_slug})"
        if "Infisical Secrets Path:" not in server.comments:
            server.comments = (server.comments or "") + infisical_reference
            server.save()
            print(f"Updated NetBox server {server.name} with Infisical reference.")
        else:
            print(f"Server {server.name} secrets synced to Infisical.")

def sync_servers_to_netbox(servers, netbox_url, netbox_token):
    nb = pynetbox.api(netbox_url, token=netbox_token)

    # We assume 'Server' role and a default Site exists, or we get them
    try:
        site = nb.dcim.sites.all()[0]
    except IndexError:
        print("No sites found in NetBox. Please create a site first.")
        return

    try:
        role = nb.dcim.device_roles.get(name="Server")
        if not role:
            role = nb.dcim.device_roles.create(name="Server", slug="server", color="2196f3")
    except Exception as e:
        print(f"Error getting/creating device role: {e}")
        return

    try:
        device_type = nb.dcim.device_types.all()[0]
    except IndexError:
        # Create a generic device type if none exists
        try:
            manufacturer = nb.dcim.manufacturers.all()[0]
        except IndexError:
            manufacturer = nb.dcim.manufacturers.create(name="Generic", slug="generic")

        device_type = nb.dcim.device_types.create(
            manufacturer=manufacturer.id,
            model="Generic Server",
            slug="generic-server"
        )

    for server in servers:
        name = server.get('name')
        ip_address = server.get('ip')
        description = server.get('description', '')

        # Check if device already exists
        device = nb.dcim.devices.get(name=name)
        if device:
            print(f"Device {name} already exists. Updating...")
            device.description = description
            device.save()
        else:
            print(f"Creating device {name}...")
            device = nb.dcim.devices.create(
                name=name,
                device_type=device_type.id,
                role=role.id,
                site=site.id,
                description=description,
                status="active"
            )

        if ip_address:
            # Create interface if it doesn't exist
            interface = nb.dcim.interfaces.get(device_id=device.id, name="eth0")
            if not interface:
                interface = nb.dcim.interfaces.create(
                    device=device.id,
                    name="eth0",
                    type="1000base-t"
                )

            # Format IP address with a CIDR suffix for Netbox if missing
            netbox_ip = ip_address if '/' in ip_address else f"{ip_address}/32"

            # Check if IP address exists
            ip = nb.ipam.ip_addresses.get(address=netbox_ip)
            if not ip:
                ip = nb.ipam.ip_addresses.create(
                    address=netbox_ip,
                    status="active"
                )

            # Assign IP to interface
            if not ip.assigned_object_id:
                ip.assigned_object_type = "dcim.interface"
                ip.assigned_object_id = interface.id
                ip.save()

            # Set primary IP for device
            if not device.primary_ip4:
                device.primary_ip4 = ip.id
                device.save()


if __name__ == "__main__":
    load_config()

    COOLIFY_URL = os.environ.get("COOLIFY_URL")
    COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN")
    NETBOX_URL = os.environ.get("NETBOX_URL")
    NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")
    NPM_URL = os.environ.get("NPM_URL")
    NPM_TOKEN = os.environ.get("NPM_TOKEN")
    PULSE_URL = os.environ.get("PULSE_URL")
    PULSE_TOKEN = os.environ.get("PULSE_TOKEN")
    INFISICAL_URL = os.environ.get("INFISICAL_URL", "https://app.infisical.com")
    INFISICAL_CLIENT_ID = os.environ.get("INFISICAL_CLIENT_ID")
    INFISICAL_CLIENT_SECRET = os.environ.get("INFISICAL_CLIENT_SECRET")
    INFISICAL_PROJECT_ID = os.environ.get("INFISICAL_PROJECT_ID")
    INFISICAL_ENV_SLUG = os.environ.get("INFISICAL_ENV_SLUG", "dev")

    if not all([COOLIFY_URL, COOLIFY_TOKEN, NETBOX_URL, NETBOX_TOKEN]):
        print("Please set COOLIFY_URL, COOLIFY_TOKEN, NETBOX_URL, and NETBOX_TOKEN environment variables.")
        exit(1)

    print("Fetching servers from Coolify...")
    try:
        servers = get_coolify_servers(COOLIFY_URL, COOLIFY_TOKEN)
        print(f"Found {len(servers)} servers in Coolify.")
        print("Syncing to NetBox...")
        sync_servers_to_netbox(servers, NETBOX_URL, NETBOX_TOKEN)
        print("Coolify Sync complete.")
    except Exception as e:
        print(f"Error during Coolify sync: {e}")

    if PULSE_URL and PULSE_TOKEN:
        print("Fetching containers from Pulse...")
        try:
            containers = get_pulse_containers(PULSE_URL, PULSE_TOKEN)
            print(f"Found {len(containers)} containers in Pulse.")
            nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
            print("Syncing Pulse containers to NetBox...")
            sync_pulse_containers_to_netbox(containers, nb)
            print("Pulse Sync complete.")
        except Exception as e:
            print(f"Error during Pulse sync: {e}")

    if NPM_URL and NPM_TOKEN:
        print("Fetching proxies from Nginx Proxy Manager...")
        try:
            proxies = get_npm_proxy_hosts(NPM_URL, NPM_TOKEN)
            print(f"Found {len(proxies)} proxies in NPM.")
            nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
            print("Syncing NPM to NetBox...")
            sync_npm_to_netbox(proxies, nb)
            print("NPM Sync complete.")
        except Exception as e:
            print(f"Error during NPM sync: {e}")

    if INFISICAL_CLIENT_ID and INFISICAL_CLIENT_SECRET and INFISICAL_PROJECT_ID:
        print("Syncing NetBox to Infisical...")
        try:
            infisical_client = InfisicalSDKClient(host=INFISICAL_URL)
            infisical_client.auth.universal_auth.login(
                client_id=INFISICAL_CLIENT_ID,
                client_secret=INFISICAL_CLIENT_SECRET
            )
            nb = pynetbox.api(NETBOX_URL, token=NETBOX_TOKEN)
            sync_netbox_to_infisical(nb, infisical_client, INFISICAL_PROJECT_ID, INFISICAL_ENV_SLUG)
            print("Infisical Sync complete.")
        except Exception as e:
            print(f"Error during Infisical sync: {e}")

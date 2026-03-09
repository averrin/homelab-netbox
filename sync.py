import os
import requests
import pynetbox

def get_coolify_servers(coolify_url, coolify_token):
    headers = {"Authorization": f"Bearer {coolify_token}"}
    url = f"{coolify_url}/api/v1/servers"
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

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
    COOLIFY_URL = os.environ.get("COOLIFY_URL")
    COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN")
    NETBOX_URL = os.environ.get("NETBOX_URL")
    NETBOX_TOKEN = os.environ.get("NETBOX_TOKEN")

    if not all([COOLIFY_URL, COOLIFY_TOKEN, NETBOX_URL, NETBOX_TOKEN]):
        print("Please set COOLIFY_URL, COOLIFY_TOKEN, NETBOX_URL, and NETBOX_TOKEN environment variables.")
        exit(1)

    print("Fetching servers from Coolify...")
    try:
        servers = get_coolify_servers(COOLIFY_URL, COOLIFY_TOKEN)
        print(f"Found {len(servers)} servers in Coolify.")
        print("Syncing to NetBox...")
        sync_servers_to_netbox(servers, NETBOX_URL, NETBOX_TOKEN)
        print("Sync complete.")
    except Exception as e:
        print(f"Error during sync: {e}")

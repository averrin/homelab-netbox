import pytest
import os
from unittest.mock import MagicMock
import sync

def test_full_execution(mocker):
    # Mock environment variables
    mocker.patch.dict(os.environ, {
        "COOLIFY_URL": "http://coolify",
        "COOLIFY_TOKEN": "coolify-token",
        "NETBOX_URL": "http://netbox",
        "NETBOX_TOKEN": "netbox-token",
        "NPM_URL": "http://npm",
        "NPM_TOKEN": "npm-token",
        "PULSE_URL": "http://pulse",
        "PULSE_TOKEN": "pulse-token",
        "INFISICAL_URL": "http://infisical",
        "INFISICAL_CLIENT_ID": "infisical-id",
        "INFISICAL_CLIENT_SECRET": "infisical-secret",
        "INFISICAL_PROJECT_ID": "infisical-project"
    }, clear=True)

    mocker.patch("sync.load_config") # bypass config loading logic since we mocked os.environ directly

    # Mock Coolify/Pulse/NPM API responses
    mock_requests_get = mocker.patch("sync.requests.get")
    def mock_get_side_effect(url, headers=None, **kwargs):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if "coolify" in url:
            resp.json.return_value = [{"name": "coolify-server-1", "ip": "10.0.0.1", "description": "test server"}]
        elif "pulse" in url:
            resp.json.return_value = [{"name": "pulse-container-1", "type": "container", "status": "running", "ipAddresses": ["10.0.0.2"]}]
        elif "nginx" in url:
            resp.json.return_value = [{"domain_names": ["example.com"], "forward_host": "10.0.0.1", "forward_port": 80, "forward_scheme": "http"}]
        else:
            resp.json.return_value = {}
        return resp
    mock_requests_get.side_effect = mock_get_side_effect

    # Mock NetBox API client
    mock_nb = MagicMock()
    mock_nb.base_url = "http://netbox"
    mocker.patch("sync.pynetbox.api", return_value=mock_nb)

    # Setup NetBox mocks to simulate existing objects (or None)
    mock_nb.dcim.sites.all.return_value = [MagicMock(id=1)]
    mock_nb.dcim.device_roles.get.return_value = MagicMock(id=1)
    mock_nb.dcim.device_types.all.return_value = [MagicMock(id=1)]

    # NetBox Device logic
    mock_nb.dcim.devices.get.return_value = None # Server doesn't exist
    mock_device = MagicMock(id=1, name="coolify-server-1", primary_ip4=MagicMock(address="10.0.0.1/32"))
    mock_device.comments = ""
    mock_nb.dcim.devices.create.return_value = mock_device
    mock_nb.dcim.devices.filter.return_value = [mock_device] # Return the device when syncing to Infisical

    # NetBox IP logic
    mock_nb.ipam.ip_addresses.get.return_value = None # IP doesn't exist
    mock_ip = MagicMock(id=1, address="10.0.0.1/32", assigned_object=MagicMock(device=MagicMock(id=1)))
    mock_nb.ipam.ip_addresses.create.return_value = mock_ip

    def ip_get_side_effect(address, **kwargs):
        ip_mock = MagicMock(id=1, address=address)
        if "10.0.0.1" in address: # Assigned to Coolify Server
            ip_mock.assigned_object.device.id = 1
        elif "10.0.0.2" in address: # Assigned to Pulse Container
            ip_mock.assigned_object.device.id = 2
        return ip_mock

    mock_nb.ipam.ip_addresses.get.side_effect = ip_get_side_effect

    # NetBox Services Logic
    mock_nb.ipam.services.get.return_value = None # Service doesn't exist

    mock_service = MagicMock(ports=[80], protocol="tcp", description="External Domains: example.com -> Internal: http://10.0.0.1:80")
    mock_nb.ipam.services.filter.return_value = [mock_service]

    # NetBox Virtualization Logic (Pulse)
    mock_nb.virtualization.cluster_types.get.return_value = MagicMock(id=1)
    mock_nb.virtualization.clusters.get.return_value = MagicMock(id=1)
    mock_nb.virtualization.virtual_machines.get.return_value = None
    mock_vm = MagicMock(id=2, name="pulse-container-1")
    mock_nb.virtualization.virtual_machines.create.return_value = mock_vm
    mock_nb.virtualization.interfaces.get.return_value = None
    mock_nb.virtualization.interfaces.create.return_value = MagicMock(id=1)

    # Mock Infisical SDK
    mock_infisical_class = mocker.patch("sync.InfisicalSDKClient")
    mock_infisical_client = MagicMock()
    mock_infisical_class.return_value = mock_infisical_client

    # Run Main Execution Block
    sync.main()

    # Verify the major steps happened
    # 1. Coolify to Netbox
    mock_nb.dcim.devices.create.assert_called_once()

    # 2. Pulse to Netbox
    mock_nb.virtualization.virtual_machines.create.assert_called_once()

    # 3. NPM to Netbox
    mock_nb.ipam.services.create.assert_called_once()

    # 4. Netbox to Infisical
    mock_infisical_client.auth.universal_auth.login.assert_called_once()
    mock_infisical_client.folders.create_folder.assert_called_once()

    assert mock_infisical_client.secrets.create_secret_by_name.call_count == 5

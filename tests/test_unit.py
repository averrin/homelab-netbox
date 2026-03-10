import pytest
import os
from unittest.mock import MagicMock, call
import pynetbox

import sync

def test_load_config_no_infisical(mocker):
    mocker.patch.dict(os.environ, clear=True)
    mocker.patch("sync.load_dotenv")
    mock_print = mocker.patch("builtins.print")
    sync.load_config()
    mock_print.assert_called_with("Infisical credentials not fully provided — relying on .env or existing environment variables.")

def test_load_config_with_infisical(mocker):
    mocker.patch.dict(os.environ, {
        "INFISICAL_CLIENT_ID": "client",
        "INFISICAL_CLIENT_SECRET": "secret",
        "INFISICAL_PROJECT_ID": "proj"
    }, clear=True)
    mocker.patch("sync.load_dotenv")
    mock_sdk = mocker.patch("sync.InfisicalSDKClient")
    mock_instance = mock_sdk.return_value

    mock_secret = MagicMock()
    mock_secret.secretKey = "TEST_KEY"
    mock_secret.secretValue = "TEST_VAL"

    mock_response = MagicMock()
    mock_response.secrets = [mock_secret]
    mock_instance.secrets.list_secrets.return_value = mock_response

    sync.load_config()

    assert os.environ["TEST_KEY"] == "TEST_VAL"

def test_get_coolify_servers(mocker):
    mock_get = mocker.patch("sync.requests.get")
    mock_response = MagicMock()
    mock_response.json.return_value = [{"id": 1}]
    mock_get.return_value = mock_response

    res = sync.get_coolify_servers("http://test", "token")
    assert res == [{"id": 1}]
    mock_get.assert_called_once_with("http://test/api/v1/servers", headers={"Authorization": "Bearer token"})

def test_get_npm_proxy_hosts(mocker):
    mock_get = mocker.patch("sync.requests.get")
    mock_response = MagicMock()
    mock_response.json.return_value = [{"id": 1}]
    mock_get.return_value = mock_response

    res = sync.get_npm_proxy_hosts("http://test", "token")
    assert res == [{"id": 1}]
    mock_get.assert_called_once_with("http://test/api/nginx/proxy-hosts", headers={"Authorization": "Bearer token"})

def test_get_pulse_containers_list(mocker):
    mock_get = mocker.patch("sync.requests.get")
    mock_response = MagicMock()
    mock_response.json.return_value = [{"type": "container", "id": 1}, {"type": "host"}]
    mock_get.return_value = mock_response

    res = sync.get_pulse_containers("http://test", "token")
    assert len(res) == 1
    assert res[0]["id"] == 1

def test_sync_servers_to_netbox(mocker):
    mock_nb = MagicMock()
    mocker.patch("sync.pynetbox.api", return_value=mock_nb)

    mock_site = MagicMock()
    mock_site.id = 1
    mock_nb.dcim.sites.all.return_value = [mock_site]

    mock_role = MagicMock()
    mock_role.id = 1
    mock_nb.dcim.device_roles.get.return_value = mock_role

    mock_dt = MagicMock()
    mock_dt.id = 1
    mock_nb.dcim.device_types.all.return_value = [mock_dt]

    mock_nb.dcim.devices.get.return_value = None
    mock_device = MagicMock()
    mock_device.id = 1
    mock_nb.dcim.devices.create.return_value = mock_device

    mock_nb.dcim.interfaces.get.return_value = None
    mock_interface = MagicMock()
    mock_interface.id = 1
    mock_nb.dcim.interfaces.create.return_value = mock_interface

    mock_nb.ipam.ip_addresses.get.return_value = None
    mock_ip = MagicMock()
    mock_ip.id = 1
    mock_nb.ipam.ip_addresses.create.return_value = mock_ip

    sync.sync_servers_to_netbox([{"name": "test-srv", "ip": "10.0.0.1"}], "http://nb", "token")

    mock_nb.dcim.devices.create.assert_called_once()
    mock_nb.dcim.interfaces.create.assert_called_once()
    mock_nb.ipam.ip_addresses.create.assert_called_once_with(address="10.0.0.1/32", status="active")

def test_sync_npm_to_netbox(mocker):
    mock_nb = MagicMock()
    mock_ip = MagicMock()
    mock_ip.assigned_object.device.id = 123
    mock_nb.ipam.ip_addresses.get.return_value = mock_ip

    mock_nb.ipam.services.get.return_value = None

    proxies = [{"domain_names": ["test.com"], "forward_host": "10.0.0.1", "forward_port": 80, "forward_scheme": "http"}]

    sync.sync_npm_to_netbox(proxies, mock_nb)

    mock_nb.ipam.services.create.assert_called_once_with(
        device=123,
        name="NPM Proxy - test.com",
        protocol="tcp",
        ports=[80],
        description="External Domains: test.com -> Internal: http://10.0.0.1:80"
    )

def test_sync_pulse_containers_to_netbox(mocker):
    mock_nb = MagicMock()
    mock_cluster_type = MagicMock()
    mock_cluster_type.id = 1
    mock_nb.virtualization.cluster_types.get.return_value = mock_cluster_type

    mock_cluster = MagicMock()
    mock_cluster.id = 1
    mock_nb.virtualization.clusters.get.return_value = mock_cluster

    mock_nb.virtualization.virtual_machines.get.return_value = None
    mock_vm = MagicMock()
    mock_vm.id = 1
    mock_nb.virtualization.virtual_machines.create.return_value = mock_vm

    mock_nb.virtualization.interfaces.get.return_value = None
    mock_interface = MagicMock()
    mock_interface.id = 1
    mock_nb.virtualization.interfaces.create.return_value = mock_interface

    mock_nb.ipam.ip_addresses.get.return_value = None

    containers = [{"name": "pulse-c", "status": "running", "ipAddresses": ["10.1.0.1"]}]
    sync.sync_pulse_containers_to_netbox(containers, mock_nb)

    mock_nb.virtualization.virtual_machines.create.assert_called_once()
    mock_nb.ipam.ip_addresses.create.assert_called_once_with(address="10.1.0.1/32", status="active")

def test_sync_netbox_to_infisical(mocker):
    mock_nb = MagicMock()
    mock_nb.base_url = "http://nb"

    mock_role = MagicMock()
    mock_role.id = 1
    mock_nb.dcim.device_roles.get.return_value = mock_role

    mock_server = MagicMock()
    mock_server.id = 1
    mock_server.name = "srv1"
    mock_server.primary_ip4.address = "10.0.0.1/32"
    mock_server.comments = ""
    mock_nb.dcim.devices.filter.return_value = [mock_server]

    mock_service = MagicMock()
    mock_service.ports = [80]
    mock_service.protocol = "tcp"
    mock_service.description = "External Domains: ext.com -> Internal: http://10.0.0.1:80"
    mock_nb.ipam.services.filter.return_value = [mock_service]

    mock_infisical = MagicMock()

    sync.sync_netbox_to_infisical(mock_nb, mock_infisical, "proj-id", "dev")

    mock_infisical.folders.create_folder.assert_called_once()

    assert mock_infisical.secrets.create_secret_by_name.call_count == 5
    assert "Infisical Secrets Path" in mock_server.comments

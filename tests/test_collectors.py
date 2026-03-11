"""Unit tests for collectors — mock HTTP/API, verify Host/Service output."""

import pytest
from unittest.mock import MagicMock, patch


class TestCoolifyCollector:
    def test_collect_fetches_apps_and_services(self):
        from config import SourceConfig
        from collectors.coolify import collect

        cfg = SourceConfig(url="http://coolify", token="test-token")

        apps_resp = MagicMock()
        apps_resp.json.return_value = [
            {
                "name": "my-app",
                "fqdn": "https://app.example.com",
                "status": "running",
                "description": "Web app",
                "destination": {"server": {"ip": "192.168.1.10"}},
            },
        ]
        apps_resp.raise_for_status = MagicMock()

        svcs_resp = MagicMock()
        svcs_resp.json.return_value = [
            {
                "name": "postgres-svc",
                "status": "running:healthy",
                "server": {"ip": "192.168.1.10"},
            },
        ]
        svcs_resp.raise_for_status = MagicMock()

        def side_effect(url, **kwargs):
            if "applications" in url:
                return apps_resp
            elif "services" in url:
                return svcs_resp
            return MagicMock()

        with patch("collectors.coolify.requests.get", side_effect=side_effect):
            hosts = collect(cfg)

        assert len(hosts) == 2
        app_host = next(h for h in hosts if h.name == "my-app")
        svc_host = next(h for h in hosts if h.name == "postgres-svc")
        assert app_host.host_type == "vm"
        assert app_host.source == "coolify"
        assert app_host.status == "active"
        assert svc_host.host_type == "vm"
        assert svc_host.cluster_name == "Coolify Services"

    def test_collect_skips_nameless(self):
        from config import SourceConfig
        from collectors.coolify import collect

        cfg = SourceConfig(url="http://coolify", token="t")
        resp = MagicMock()
        resp.json.return_value = [{"status": "running"}]  # no name
        resp.raise_for_status = MagicMock()

        with patch("collectors.coolify.requests.get", return_value=resp):
            hosts = collect(cfg)

        assert len(hosts) == 0

    def test_collect_raises_on_missing_credentials(self):
        from config import SourceConfig
        from collectors.coolify import collect

        with pytest.raises(RuntimeError, match="COOLIFY_URL"):
            collect(SourceConfig(url="", token="t"))

        with pytest.raises(RuntimeError, match="COOLIFY_URL"):
            collect(None)


class TestPulseCollector:
    def test_collect_filters_docker_containers(self):
        """Pulse unified resource type 'docker-container' is collected."""
        from config import SourceConfig
        from collectors.pulse import collect

        cfg = SourceConfig(url="http://pulse", token="t")
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "id": "abc123",
                "name": "my-container",
                "displayName": "My Container",
                "type": "docker-container",
                "status": "running",
                "identity": {"ips": ["10.0.0.1"]},
            },
            {"name": "pve-node", "type": "node", "status": "online"},
            {"name": "my-vm", "type": "vm", "status": "running"},
        ]
        mock_resp.raise_for_status = MagicMock()

        with patch("collectors.pulse.requests.get", return_value=mock_resp):
            hosts = collect(cfg)

        assert len(hosts) == 1
        assert hosts[0].name == "My Container"  # displayName preferred
        assert hosts[0].host_type == "container"
        assert hosts[0].status == "active"
        assert "10.0.0.1" in hosts[0].get_all_ips()

    def test_collect_filters_lxc_containers(self):
        """Pulse type 'container' (LXC) is also collected."""
        from config import SourceConfig
        from collectors.pulse import collect

        cfg = SourceConfig(url="http://pulse", token="t")
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"name": "lxc1", "type": "container", "status": "running"},
        ]
        mock_resp.raise_for_status = MagicMock()

        with patch("collectors.pulse.requests.get", return_value=mock_resp):
            hosts = collect(cfg)

        assert len(hosts) == 1
        assert hosts[0].name == "lxc1"

    def test_collect_handles_dict_response(self):
        from config import SourceConfig
        from collectors.pulse import collect

        cfg = SourceConfig(url="http://pulse", token="t")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": [
                {"name": "dc1", "type": "docker-container", "status": "running"},
            ]
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("collectors.pulse.requests.get", return_value=mock_resp):
            hosts = collect(cfg)

        assert len(hosts) == 1

    def test_collect_fallback_to_legacy_ip_field(self):
        """Falls back to ipAddresses when identity.ips is absent."""
        from config import SourceConfig
        from collectors.pulse import collect

        cfg = SourceConfig(url="http://pulse", token="t")
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "name": "old-container",
                "type": "docker-container",
                "status": "running",
                "ipAddresses": ["192.168.1.5"],
            },
        ]
        mock_resp.raise_for_status = MagicMock()

        with patch("collectors.pulse.requests.get", return_value=mock_resp):
            hosts = collect(cfg)

        assert len(hosts) == 1
        assert "192.168.1.5" in hosts[0].get_all_ips()

    def test_collect_raises_on_missing_credentials(self):
        from config import SourceConfig
        from collectors.pulse import collect

        with pytest.raises(RuntimeError, match="PULSE_URL"):
            collect(None)


class TestNpmCollector:
    def test_collect_returns_services(self):
        from config import NpmConfig
        from collectors.npm import collect

        cfg = NpmConfig(url="http://npm", email="a@b.com", password="pass")
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"token": "jwt-token-123"}

        proxy_resp = MagicMock()
        proxy_resp.json.return_value = [
            {
                "domain_names": ["example.com", "www.example.com"],
                "forward_host": "192.168.1.20",
                "forward_port": 8080,
                "forward_scheme": "http",
            },
        ]
        proxy_resp.raise_for_status = MagicMock()

        def side_effect(url, **kwargs):
            return proxy_resp

        with patch("collectors.npm.requests.post", return_value=login_resp):
            with patch("collectors.npm.requests.get", side_effect=side_effect) as mock_get:
                services = collect(cfg)

        assert len(services) == 1
        svc = services[0]
        assert svc.name == "NPM Proxy - example.com"
        assert svc.forward_host == "192.168.1.20"
        assert svc.ports == [8080]
        assert "https://example.com" in svc.external_urls
        assert "https://www.example.com" in svc.external_urls
        # Verify token was used in proxy-hosts request
        mock_get.assert_called_once_with(
            "http://npm/api/nginx/proxy-hosts",
            headers={"Authorization": "Bearer jwt-token-123"},
            verify=False,
        )

    def test_collect_login_sends_identity_secret(self):
        """Verify NPM login sends email as 'identity' and password as 'secret'."""
        from config import NpmConfig
        from collectors.npm import collect

        cfg = NpmConfig(url="http://npm", email="admin@test.com", password="s3cret")
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"token": "tok"}

        proxy_resp = MagicMock()
        proxy_resp.json.return_value = []
        proxy_resp.raise_for_status = MagicMock()

        with patch("collectors.npm.requests.post", return_value=login_resp) as mock_post:
            with patch("collectors.npm.requests.get", return_value=proxy_resp):
                collect(cfg)

        mock_post.assert_called_once_with(
            "http://npm/api/tokens",
            json={"identity": "admin@test.com", "secret": "s3cret"},
            verify=False,
        )

    def test_collect_raises_on_login_failure(self):
        from config import NpmConfig
        from collectors.npm import collect

        cfg = NpmConfig(url="http://npm", email="a@b.com", password="bad")
        login_resp = MagicMock()
        login_resp.status_code = 401
        login_resp.text = "Invalid credentials"

        with patch("collectors.npm.requests.post", return_value=login_resp):
            with pytest.raises(RuntimeError, match="NPM login failed"):
                collect(cfg)

    def test_collect_raises_on_missing_credentials(self):
        from config import NpmConfig
        from collectors.npm import collect

        with pytest.raises(RuntimeError, match="NPM_URL"):
            collect(None)

    def test_collect_skips_incomplete(self):
        from config import NpmConfig
        from collectors.npm import collect

        cfg = NpmConfig(url="http://npm", email="a@b.com", password="p")
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"token": "tok"}

        proxy_resp = MagicMock()
        proxy_resp.json.return_value = [
            {"domain_names": [], "forward_host": "10.0.0.1", "forward_port": 80},
            {"domain_names": ["x.com"], "forward_port": 80},  # no forward_host
        ]
        proxy_resp.raise_for_status = MagicMock()

        with patch("collectors.npm.requests.post", return_value=login_resp):
            with patch("collectors.npm.requests.get", return_value=proxy_resp):
                services = collect(cfg)

        assert len(services) == 0

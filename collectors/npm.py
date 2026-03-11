"""NPM collector — fetches proxy hosts from Nginx Proxy Manager.

Authenticates via email/password (POST /api/tokens) since NPM has no
UI for generating persistent API tokens.

Unlike other collectors, NPM produces Service objects (not Hosts).
Services carry a forward_host IP so the merger can attach them to the right host.
"""

from __future__ import annotations

import requests

from config import NpmConfig
from models import Service


def _login(cfg: NpmConfig) -> str:
    """Authenticate with NPM and return a bearer token.

    Raises:
        RuntimeError: If login fails.
    """
    url = f"{cfg.url}/api/tokens"
    payload = {"identity": cfg.email, "secret": cfg.password}
    response = requests.post(url, json=payload, verify=False)

    if response.status_code != 200:
        raise RuntimeError(
            f"NPM login failed (HTTP {response.status_code}): {response.text}"
        )

    data = response.json()
    token = data.get("token")
    if not token:
        raise RuntimeError(f"NPM login response missing 'token': {data}")

    return token


def collect(cfg: NpmConfig) -> list[Service]:
    """Login to NPM, fetch proxy hosts, and return Service objects.

    Raises:
        RuntimeError: If credentials are missing or login fails.
    """
    if not cfg or not cfg.url or not cfg.email or not cfg.password:
        raise RuntimeError(
            "NPM collector requires NPM_URL, NPM_EMAIL, and NPM_PASSWORD."
        )

    token = _login(cfg)

    headers = {"Authorization": f"Bearer {token}"}
    url = f"{cfg.url}/api/nginx/proxy-hosts"
    response = requests.get(url, headers=headers, verify=False)
    response.raise_for_status()

    services: list[Service] = []
    for proxy in response.json():
        domain_names = proxy.get("domain_names", [])
        forward_ip = proxy.get("forward_host")
        forward_port = proxy.get("forward_port")
        forward_scheme = proxy.get("forward_scheme", "http")

        if not forward_ip or not domain_names:
            continue

        main_domain = domain_names[0]
        external_urls = [f"https://{d}" for d in domain_names]
        description = (
            f"External: {', '.join(domain_names)} → "
            f"Internal: {forward_scheme}://{forward_ip}:{forward_port}"
        )

        services.append(
            Service(
                name=f"NPM Proxy - {main_domain}",
                protocol="tcp",
                ports=[forward_port],
                description=description,
                external_urls=external_urls,
                internal_urls=[f"{forward_scheme}://{forward_ip}:{forward_port}"],
                forward_host=forward_ip,
            )
        )

    return services

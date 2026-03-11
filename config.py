"""Centralized configuration loader.

Loads secrets from Infisical (if credentials present) or falls back to .env.
Returns a typed Config object so modules don't touch os.environ directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv


@dataclass
class SourceConfig:
    url: str
    token: str


@dataclass
class ProxmoxConfig:
    host: str
    user: str
    token_name: str
    token_secret: str
    cluster_name: str = "Proxmox"
    cluster_id: int = 1


@dataclass
class NpmConfig:
    url: str
    email: str
    password: str


@dataclass
class InfisicalConfig:
    url: str = "https://app.infisical.com"
    client_id: str = ""
    client_secret: str = ""
    project_id: str = ""
    environment: str = "prod"
    secret_path: str = "/"
    org_id: str = ""
    project_slug: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.project_id)


@dataclass
class Config:
    netbox: SourceConfig | None = None
    coolify: SourceConfig | None = None
    pulse: SourceConfig | None = None
    npm: NpmConfig | None = None
    proxmox: list[ProxmoxConfig] = field(default_factory=list)
    infisical: InfisicalConfig = field(default_factory=InfisicalConfig)

    def available_sources(self) -> list[str]:
        """Return list of source names that have credentials configured."""
        sources = []
        if self.coolify:
            sources.append("coolify")
        if self.proxmox:
            sources.append("proxmox")
        if self.pulse:
            sources.append("pulse")
        if self.npm:
            sources.append("npm")
        return sources


def _inject_infisical_secrets():
    """Load secrets from Infisical into os.environ if credentials are available."""
    client_id = os.environ.get("INFISICAL_CLIENT_ID")
    client_secret = os.environ.get("INFISICAL_CLIENT_SECRET")
    project_id = os.environ.get("INFISICAL_PROJECT_ID")

    if not client_id or not client_secret:
        print("Infisical credentials not provided — relying on .env / environment variables.")
        return

    if not project_id:
        print("INFISICAL_PROJECT_ID is required to fetch configuration secrets.")
        return

    environment = os.environ.get("INFISICAL_ENVIRONMENT", "prod")
    secret_path = os.environ.get("INFISICAL_SECRET_PATH", "/")
    site_url = os.environ.get("INFISICAL_URL", "https://app.infisical.com").rstrip("/")

    try:
        from infisical_sdk import InfisicalSDKClient

        client = InfisicalSDKClient(host=site_url)
        client.auth.universal_auth.login(client_id=client_id, client_secret=client_secret)
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
        print(f"Failed to fetch secrets from Infisical: {e}")


def _opt_source(url_var: str, token_var: str) -> SourceConfig | None:
    url = os.environ.get(url_var)
    token = os.environ.get(token_var)
    if url and token:
        return SourceConfig(url=url.rstrip("/"), token=token)
    return None


def load_config() -> Config:
    """Load .env, optionally inject Infisical secrets, and build a Config object."""
    load_dotenv()
    _inject_infisical_secrets()

    netbox = _opt_source("NETBOX_URL", "NETBOX_TOKEN")
    if not netbox:
        raise ValueError("NETBOX_URL and NETBOX_TOKEN are required.")

    # Proxmox
    proxmox_configs: list[ProxmoxConfig] = []
    
    # 1. Check for basic unsuffixed variables
    pve_host_raw = os.environ.get("PVE_API_HOST")
    pve_user = os.environ.get("PVE_API_USER")
    pve_token = os.environ.get("PVE_API_TOKEN")
    pve_secret = os.environ.get("PVE_API_SECRET")
    
    if pve_host_raw and pve_user and pve_token and pve_secret:
        pve_host = pve_host_raw.replace("https://", "").replace("http://", "").rstrip("/")
        token_name = pve_token.split("!")[-1]
        
        proxmox_configs.append(ProxmoxConfig(
            host=pve_host,
            user=pve_user,
            token_name=token_name,
            token_secret=pve_secret,
            cluster_name=os.environ.get("PVE_CLUSTER_NAME", "Proxmox"),
            cluster_id=int(os.environ.get("NB_CLUSTER_ID", "1")),
        ))

    # 2. Check for suffixed variables (_1, _2, etc.)
    i = 1
    while True:
        pve_host_raw = os.environ.get(f"PVE_API_HOST_{i}")
        pve_user = os.environ.get(f"PVE_API_USER_{i}")
        pve_token = os.environ.get(f"PVE_API_TOKEN_{i}")
        pve_secret = os.environ.get(f"PVE_API_SECRET_{i}")
        
        if pve_host_raw and pve_user and pve_token and pve_secret:
            pve_host = pve_host_raw.replace("https://", "").replace("http://", "").rstrip("/")
            token_name = pve_token.split("!")[-1]
            
            proxmox_configs.append(ProxmoxConfig(
                host=pve_host,
                user=pve_user,
                token_name=token_name,
                token_secret=pve_secret,
                cluster_name=os.environ.get(f"PVE_CLUSTER_NAME_{i}", f"Proxmox {i}"),
                cluster_id=int(os.environ.get(f"NB_CLUSTER_ID_{i}", str(i))),
            ))
            i += 1
        else:
            break

    # NPM
    npm = None
    npm_url = os.environ.get("NPM_URL")
    npm_email = os.environ.get("NPM_EMAIL")
    npm_password = os.environ.get("NPM_PASSWORD")
    if npm_url and npm_email and npm_password:
        npm = NpmConfig(
            url=npm_url.rstrip("/"),
            email=npm_email,
            password=npm_password,
        )

    return Config(
        netbox=netbox,
        coolify=_opt_source("COOLIFY_URL", "COOLIFY_TOKEN"),
        pulse=_opt_source("PULSE_URL", "PULSE_TOKEN"),
        npm=npm,
        proxmox=proxmox_configs,
        infisical=InfisicalConfig(
            url=os.environ.get("INFISICAL_URL", "https://app.infisical.com"),
            client_id=os.environ.get("INFISICAL_CLIENT_ID", ""),
            client_secret=os.environ.get("INFISICAL_CLIENT_SECRET", ""),
            project_id=os.environ.get("INFISICAL_PROJECT_ID", ""),
            environment=os.environ.get("INFISICAL_ENV_SLUG", "prod"),
            secret_path=os.environ.get("INFISICAL_SECRET_PATH", "/"),
            org_id=os.environ.get("INFISICAL_ORG_ID", "605b759e-1f28-4ba0-9e68-c9d050e519ca"),
            project_slug=os.environ.get("INFISICAL_PROJECT_SLUG", "secret-management"),
        ),
    )

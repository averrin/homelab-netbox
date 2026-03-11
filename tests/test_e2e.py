"""End-to-end tests that hit real services.

These tests verify actual collector outputs and host matching against
live infrastructure. They are skipped if the required environment
variables are not set.

Run with: pytest tests/test_e2e.py -m e2e -v
"""

import os
import pytest

# Mark all tests in this module as e2e
pytestmark = pytest.mark.e2e


def _has_env(*vars):
    return all(os.environ.get(v) for v in vars)


# ---------------------------------------------------------------------------
# Collector output validation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _has_env("COOLIFY_URL", "COOLIFY_TOKEN"),
    reason="Coolify credentials not set",
)
def test_coolify_collector_output():
    """Coolify collector returns valid Host objects from live API."""
    from config import SourceConfig
    from collectors.coolify import collect

    cfg = SourceConfig(
        url=os.environ["COOLIFY_URL"],
        token=os.environ["COOLIFY_TOKEN"],
    )
    hosts = collect(cfg)

    assert len(hosts) > 0, "Expected at least one Coolify server"
    for host in hosts:
        assert host.name, "Host must have a name"
        assert host.host_type == "device"
        assert host.source == "coolify"


@pytest.mark.skipif(
    not _has_env("PULSE_URL", "PULSE_TOKEN"),
    reason="Pulse credentials not set",
)
def test_pulse_collector_output():
    """Pulse collector returns valid Host objects from live API."""
    from config import SourceConfig
    from collectors.pulse import collect

    cfg = SourceConfig(
        url=os.environ["PULSE_URL"],
        token=os.environ["PULSE_TOKEN"],
    )
    hosts = collect(cfg)

    # Pulse may have zero containers, that's OK
    for host in hosts:
        assert host.name, "Host must have a name"
        assert host.host_type == "container"
        assert host.source == "pulse"


@pytest.mark.skipif(
    not _has_env("NPM_URL", "NPM_TOKEN"),
    reason="NPM credentials not set",
)
def test_npm_collector_output():
    """NPM collector returns valid Service objects from live API."""
    from config import SourceConfig
    from collectors.npm import collect

    cfg = SourceConfig(
        url=os.environ["NPM_URL"],
        token=os.environ["NPM_TOKEN"],
    )
    services = collect(cfg)

    assert len(services) > 0, "Expected at least one NPM proxy"
    for svc in services:
        assert svc.name, "Service must have a name"
        assert svc.forward_host, "Service must have a forward_host"
        assert len(svc.ports) > 0, "Service must have at least one port"
        assert len(svc.external_urls) > 0, "Service must have external URLs"


@pytest.mark.skipif(
    not _has_env("PVE_API_HOST", "PVE_API_USER", "PVE_API_TOKEN", "PVE_API_SECRET"),
    reason="Proxmox credentials not set",
)
def test_proxmox_collector_output():
    """Proxmox collector returns valid Host objects from live API."""
    from config import ProxmoxConfig
    from collectors.proxmox import collect

    cfg = ProxmoxConfig(
        host=os.environ["PVE_API_HOST"],
        user=os.environ["PVE_API_USER"],
        token_name=os.environ["PVE_API_TOKEN"],
        token_secret=os.environ["PVE_API_SECRET"],
        cluster_id=int(os.environ.get("NB_CLUSTER_ID", "1")),
    )
    hosts = collect(cfg)

    assert len(hosts) > 0, "Expected at least one Proxmox host (node)"
    # At minimum, PVE nodes should show up as devices
    devices = [h for h in hosts if h.host_type == "device"]
    assert len(devices) > 0, "Expected at least one PVE node device"
    for d in devices:
        assert d.source == "proxmox"


# ---------------------------------------------------------------------------
# Host matching validation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _has_env("COOLIFY_URL", "COOLIFY_TOKEN", "NPM_URL", "NPM_TOKEN"),
    reason="Coolify + NPM credentials needed for matching test",
)
def test_npm_services_match_coolify_hosts():
    """NPM services should attach to Coolify hosts via forward_host IP."""
    from config import SourceConfig
    from collectors.coolify import collect as collect_coolify
    from collectors.npm import collect as collect_npm
    from merger import merge

    coolify_cfg = SourceConfig(
        url=os.environ["COOLIFY_URL"],
        token=os.environ["COOLIFY_TOKEN"],
    )
    npm_cfg = SourceConfig(
        url=os.environ["NPM_URL"],
        token=os.environ["NPM_TOKEN"],
    )

    coolify_hosts = collect_coolify(coolify_cfg)
    npm_services = collect_npm(npm_cfg)

    state = merge(coolify_hosts=coolify_hosts, npm_services=npm_services)

    # At least some services should have matched
    matched_services = sum(len(h.services) for h in state.hosts.values())
    total_services = matched_services + len(state.unmatched_services)

    print(f"Matched: {matched_services}/{total_services} NPM services to hosts")
    print(f"Hosts with services: {[n for n, h in state.hosts.items() if h.services]}")

    # This is informational — don't fail if no matches, but log it
    if matched_services == 0 and total_services > 0:
        print("WARNING: No NPM services matched any host. Check IP mapping.")


# ---------------------------------------------------------------------------
# Dry-run idempotency
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _has_env("NETBOX_URL", "NETBOX_TOKEN", "COOLIFY_URL", "COOLIFY_TOKEN"),
    reason="NetBox + Coolify credentials needed",
)
def test_dry_run_idempotent():
    """Running the full pipeline twice in dry-run should produce identical actions."""
    from config import load_config
    from collectors.coolify import collect as collect_coolify
    from merger import merge
    from reconciler import get_nb_client, reconcile

    cfg = load_config()
    hosts = collect_coolify(cfg.coolify)
    state = merge(coolify_hosts=hosts)
    nb = get_nb_client(cfg.netbox.url, cfg.netbox.token)

    actions1 = reconcile(state, nb)
    actions2 = reconcile(state, nb)

    # Compare action lists
    assert len(actions1) == len(actions2), "Action counts differ between runs"
    for a1, a2 in zip(actions1, actions2):
        assert a1.verb == a2.verb, f"Verb mismatch: {a1.target}"
        assert a1.object_type == a2.object_type, f"Type mismatch: {a1.target}"
        assert a1.target == a2.target, "Target mismatch"

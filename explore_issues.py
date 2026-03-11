import os
from config import load_config
from collectors import proxmox, coolify
from merger import merge

cfg = load_config()

print("--- Issue 5: Proxmox Clusters ---")
for pcfg in cfg.proxmox:
    print(f"Proxmox Config: {pcfg.host}")
    # Let's see if we can get cluster name
    from proxmoxer import ProxmoxAPI
    pve = ProxmoxAPI(
        host=pcfg.host,
        user=pcfg.user,
        token_name=pcfg.token_name,
        token_value=pcfg.token_secret,
        verify_ssl=False,
    )
    try:
        cluster_status = pve.cluster.status.get()
        print(f"  Cluster status: {cluster_status}")
    except Exception as e:
        print(f"  Error getting cluster status: {e}")

print("\n--- Collecting Data ---")
p_hosts = []
for pcfg in cfg.proxmox:
    p_hosts.extend(proxmox.collect(pcfg))
    
c_hosts = []
if cfg.coolify:
    c_hosts = coolify.collect(cfg.coolify)

print("\n--- Issue 1 & 4: Proxmox VMs & LXC (cloudflared, etc) ---")
for h in p_hosts:
    if h.name in ["cloudflared", "pihole"]:
        print(f"{h.name}: type={h.host_type}, platform={h.platform}, cluster={h.cluster_name}")
        print(f"  config_url={h.config_url}")
        print(f"  internal_url={h.internal_url}")
        print(f"  tags={h.tags}")
        print(f"  ips={[ip.address for iface in h.interfaces for ip in iface.ip_addresses]}")

print("\n--- Issue 6: Coolify Applications ---")
for h in c_hosts:
    if "buxfer" in h.name.lower() or "infisical" in h.name.lower():
        print(f"{h.name}: type={h.host_type}, cluster={h.cluster_name}")
        print(f"  config_url={h.config_url}")
        print(f"  internal_url={h.internal_url}")
        print(f"  ips={[ip.address for iface in h.interfaces for ip in iface.ip_addresses]}")

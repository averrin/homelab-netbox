import pynetbox
from config import load_config

cfg = load_config()
nb = pynetbox.api(url=cfg.netbox.url, token=cfg.netbox.token)
nb.http_session.verify = False

def check_vm(name):
    vms = list(nb.virtualization.virtual_machines.filter(name=name))
    print(f"\n--- Checking {name} ({len(vms)} found) ---")
    for vm in vms:
        print(f"ID: {vm.id}, Platform: {vm.platform.name if vm.platform else None}, Cluster: {vm.cluster.name if vm.cluster else None}")
        print(f"Primary IP: {vm.primary_ip4}")
        print(f"Tags: {[t.name for t in getattr(vm, 'tags', [])]}")
        
check_vm("cloudflared")
check_vm("nocodb")
check_vm("buxfer-mcp")

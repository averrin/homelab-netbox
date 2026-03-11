"""Unit tests for the Reconciler deletion and protection logic."""

import pytest
from models import DesiredState, Host, Action
from reconciler import reconcile

class MockNB:
    """Mock NetBox client matching pynetbox structure."""
    def __init__(self, devices=None, vms=None, services=None):
        self.dcim = MockApp(devices or [], "devices")
        self.virtualization = MockApp(vms or [], "virtual_machines")
        self.ipam = MockApp(services or [], "services")

class MockApp:
    def __init__(self, items, name):
        setattr(self, name, self)
        self.items = items
    def all(self):
        return self.items

class MockObj:
    def __init__(self, name, id, custom_fields=None, tags=None, status="active", description="", type="device"):
        self.name = name
        self.id = id
        self.custom_fields = MockCF(custom_fields or {})
        self.tags = [MockTag(t) for t in (tags or [])]
        self.status = MockStatus(status)
        self.description = description
        # For internal use in mocks
        self._type = type
        
    @property
    def device(self): 
        return MockObj("host", 1) if self._type == "service" else None
    @property
    def virtual_machine(self): 
        return None

class MockCF:
    def __init__(self, data):
        self._data = data
    def get(self, key, default=None):
        return self._data.get(key, default)
    def __getitem__(self, key):
        return self._data[key]
    def __getattr__(self, key):
        # Support dotted access for getattr(svc.custom_fields, ...)
        if key in self._data:
            return self._data[key]
        return False

class MockTag:
    def __init__(self, name):
        self.name = name

class MockStatus:
    def __init__(self, value):
        self.value = value

def test_delete_stray_hosts():
    """Verify that hosts in NetBox but not in desired state are deleted."""
    # NetBox has 'server1' and 'server2'
    nb = MockNB(devices=[
        MockObj("server1", 1),
        MockObj("server2", 2)
    ])
    
    # Desired state only has 'server1'
    desired = DesiredState(hosts={
        "server1": Host(name="server1", host_type="device")
    })
    
    actions = reconcile(desired, nb)
    
    # Should see DELETE for server2
    deletes = [a for a in actions if a.verb == "delete" and a.target == "server2"]
    assert len(deletes) == 1
    assert deletes[0].object_type == "device"

def test_protected_host_skips_deletion():
    """Verify that protected NetBox hosts are not deleted."""
    # NetBox has 'protected-server' marked as protected
    nb = MockNB(devices=[
        MockObj("protected-server", 1, custom_fields={"netbox_sync_protected": True})
    ])
    
    # Desired state is empty
    desired = DesiredState(hosts={})
    
    actions = reconcile(desired, nb)
    
    # Should see SKIP for protected-server
    skips = [a for a in actions if a.verb == "skip" and a.target == "protected-server"]
    assert len(skips) == 1
    assert "Protected" in skips[0].reason

def test_deprecated_services_all_deleted():
    """Verify that all services are marked for deletion (since model is simplified)."""
    # NetBox has 2 services
    nb = MockNB(services=[
        MockObj("HTTP", 10, type="service"),
        MockObj("SSH", 11, type="service")
    ])
    
    # Desired state (model no longer has services)
    desired = DesiredState(hosts={
        "host": Host(name="host", host_type="device")
    })
    
    actions = reconcile(desired, nb)
    
    # Should see 2 deletions for services
    deletes = [a for a in actions if a.verb == "delete" and a.object_type == "service"]
    assert len(deletes) == 2

def test_url_metadata_enrichment():
    """Verify that singular URL fields are reconciled."""
    nb = MockNB(devices=[
        MockObj("server1", 1, custom_fields={"external_url": "http://old.com"})
    ])
    
    desired = DesiredState(hosts={
        "server1": Host(
            name="server1", 
            host_type="device",
            external_url="https://new.com"
        )
    })
    
    actions = reconcile(desired, nb)
    
    update = [a for a in actions if a.verb == "update" and a.target == "server1"][0]
    assert update.details["custom_fields"]["external_url"] == "https://new.com"

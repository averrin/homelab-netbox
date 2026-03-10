import pytest
import os
import sync

@pytest.mark.skipif(
    not os.environ.get("COOLIFY_URL") or not os.environ.get("NETBOX_URL"),
    reason="Live environment variables (COOLIFY_URL, NETBOX_URL) are not set. Skipping true E2E test."
)
def test_live_end_to_end_sync():
    """
    This test runs the actual sync.py script against live servers.
    It requires the environment variables for Coolify, NetBox, NPM, Pulse, and Infisical to be populated
    either via a .env file or by the environment directly.
    """
    # Simply call the main execution block
    try:
        sync.main()
    except Exception as e:
        pytest.fail(f"Live E2E synchronization failed with error: {e}")

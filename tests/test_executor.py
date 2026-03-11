"""Unit tests for the executor — dry-run output formatting."""

import io
import sys

from models import Action
from executor import execute


def test_dry_run_prints_create(capsys):
    """Dry run should print CREATE actions."""
    actions = [
        Action(
            verb="create",
            object_type="device",
            target="test-server",
            details={"name": "test-server", "status": "active", "role": "Server"},
        )
    ]
    # Use a mock nb since dry_run won't call it
    execute(actions, nb=None, dry_run=True, verbose=False)
    output = capsys.readouterr().out
    assert "CREATE" in output
    assert "test-server" in output
    assert "1 create" in output


def test_dry_run_prints_update_diff(capsys):
    """Dry run should show old → new values for updates."""
    actions = [
        Action(
            verb="update",
            object_type="vm",
            target="my-vm",
            details={"status": {"old": "offline", "new": "active"}},
        )
    ]
    execute(actions, nb=None, dry_run=True, verbose=False)
    output = capsys.readouterr().out
    assert "UPDATE" in output
    assert "offline" in output
    assert "active" in output


def test_dry_run_hides_skips_by_default(capsys):
    """Skips should be hidden unless verbose."""
    actions = [
        Action(verb="skip", object_type="device", target="unchanged", reason="no changes")
    ]
    execute(actions, nb=None, dry_run=True, verbose=False)
    output = capsys.readouterr().out
    assert "unchanged" not in output
    assert "1 skip" in output  # summary still shows


def test_dry_run_shows_skips_when_verbose(capsys):
    """Verbose mode should show skips."""
    actions = [
        Action(verb="skip", object_type="device", target="unchanged", reason="no changes")
    ]
    execute(actions, nb=None, dry_run=True, verbose=True)
    output = capsys.readouterr().out
    assert "unchanged" in output
    assert "no changes" in output


def test_summary_counts(capsys):
    """Summary line should have correct counts."""
    actions = [
        Action(verb="create", object_type="device", target="a", details={"name": "a"}),
        Action(verb="update", object_type="vm", target="b", details={"x": {"old": 1, "new": 2}}),
        Action(verb="skip", object_type="ip", target="c", reason="exists"),
        Action(verb="skip", object_type="ip", target="d", reason="exists"),
    ]
    execute(actions, nb=None, dry_run=True, verbose=False)
    output = capsys.readouterr().out
    assert "1 create" in output
    assert "1 update" in output
    assert "2 skip" in output

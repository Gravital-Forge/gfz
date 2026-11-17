"""Tests for gfz's provider-neutral host vocabulary."""

import dataclasses
from collections.abc import Sequence

import pytest

from gfz.hosts import ActionStatus, Host, HostClient, PowerAction


def _make_host(**overrides: object) -> Host:
    """Build a Host with reasonable defaults, overridden by keyword."""
    fields = {
        "id": 1,
        "name": "agent-host-1",
        "status": "active",
        "region": "tor1",
        "size": "s-4vcpu-8gb",
        "ip": "203.0.113.5",
        "created_at": "2026-01-01T00:00:00Z",
        "tags": ("gfz",),
    }
    fields.update(overrides)
    return Host(**fields)


def test_host_is_frozen():
    """Assigning to a Host field raises FrozenInstanceError."""
    host = _make_host()

    with pytest.raises(dataclasses.FrozenInstanceError):
        host.name = "renamed"


def test_hosts_with_equal_fields_compare_equal():
    """Two Host values built from the same field values compare equal."""
    first = _make_host()
    second = _make_host()

    assert first == second


def test_host_is_hashable():
    """A Host is hashable: it can go in a set or serve as a dict key."""
    host = _make_host()

    hosts_seen = {host}
    counts_by_host = {host: 1}

    assert host in hosts_seen
    assert counts_by_host[host] == 1


def test_host_accepts_none_for_ip():
    """A Host accepts None for ip, since a new host has no address yet."""
    host = _make_host(ip=None)

    assert host.ip is None


class _FakeHostClient:
    """A canned HostClient that reaches no network."""

    def __init__(self) -> None:
        """Set up the fake's canned host and account keys."""
        self._host = _make_host()
        self._account_keys = ("laptop", "deploy-key")

    def create_host(
        self,
        *,
        name: str,
        region: str,
        size: str,
        image: str,
        ssh_keys: Sequence[str],
        tags: Sequence[str],
    ) -> Host:
        """Return the canned host, ignoring the requested attributes."""
        return self._host

    def get_host(self, host_id: int) -> Host:
        """Return the canned host, ignoring the requested id."""
        return self._host

    def list_hosts(self, *, tag: str | None = None) -> Sequence[Host]:
        """Return the canned host as a single-item sequence."""
        return (self._host,)

    def delete_host(self, host_id: int) -> None:
        """Do nothing; the fake reaches no provider to delete from."""

    def power_host(self, host_id: int, action: PowerAction) -> ActionStatus:
        """Return a completed status, ignoring the requested action."""
        return "completed"

    def list_account_keys(self) -> Sequence[str]:
        """Return the canned account key names."""
        return self._account_keys


def account_key_names(client: HostClient) -> Sequence[str]:
    """Return the account key names a HostClient reports."""
    return client.list_account_keys()


def test_fake_client_satisfies_host_client_protocol():
    """A fake implementing every HostClient method is accepted as a HostClient."""
    fake = _FakeHostClient()

    names = account_key_names(fake)

    assert names == ("laptop", "deploy-key")


def test_fake_client_drives_all_six_operations():
    """The fake answers create, get, list, delete, power, and key listing."""
    fake = _FakeHostClient()

    created = fake.create_host(
        name="agent-host-1",
        region="tor1",
        size="s-4vcpu-8gb",
        image="ubuntu-24-04-x64",
        ssh_keys=["laptop"],
        tags=["gfz"],
    )
    fetched = fake.get_host(created.id)
    listed = fake.list_hosts(tag="gfz")
    status = fake.power_host(created.id, "power_on")
    fake.delete_host(created.id)

    assert created == fetched
    assert listed == (created,)
    assert status == "completed"

"""The provider-neutral host vocabulary gfz's commands and a provider client share."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

HostStatus = Literal["new", "active", "off", "archive"]
PowerAction = Literal["power_on", "shutdown", "power_off"]
ActionStatus = Literal["in-progress", "completed", "errored"]


@dataclass(frozen=True)
class Host:
    """One host as any provider describes it.

    Attributes:
        id: The provider's numeric identifier for the host.
        name: The host's name.
        status: The host's lifecycle status.
        region: The region the host runs in.
        size: The host's size slug.
        ip: The host's public IPv4 address, or `None` if it has not been given
            one yet. A freshly created host has no address until it becomes
            active.
        created_at: The provider's ISO 8601 timestamp string for when the host
            was created.
        tags: The tags attached to the host. A tuple, since the record is
            frozen.
    """

    id: int
    name: str
    status: HostStatus
    region: str
    size: str
    ip: str | None
    created_at: str
    tags: tuple[str, ...]


class HostClient(Protocol):
    """The host operations gfz's commands depend on.

    Two implementations exist from the day this is written: the real
    provider client and a test fake. Depending on this protocol, rather than
    a provider module, is what keeps the command layer free of any provider
    payload.
    """

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
        """Create a host and return it as the provider first reports it.

        `ssh_keys` holds the operator's configured key identifiers — a
        numeric id, a fingerprint, or an account key's name — and the
        implementation resolves them. For a new host, the returned value has
        status `new` and no address yet.
        """
        ...

    def get_host(self, host_id: int) -> Host:
        """Return the host with this id.

        This is what a caller polls while it waits for an address.
        """
        ...

    def list_hosts(self, *, tag: str | None = None) -> Sequence[Host]:
        """Return every host the account holds, or only those carrying `tag`."""
        ...

    def delete_host(self, host_id: int) -> None:
        """Delete the host with this id."""
        ...

    def power_host(self, host_id: int, action: PowerAction) -> ActionStatus:
        """Run the power action and return its status as the provider reports it."""
        ...

    def list_account_keys(self) -> Sequence[str]:
        """Return the name of every SSH key the account holds."""
        ...

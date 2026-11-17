"""The DigitalOcean provider client: the only module aware of its API.

Every DigitalOcean field, path, and status code lives here. `DigitalOceanClient`
implements `gfz.hosts.HostClient` and returns `gfz.hosts.Host` values; nothing
it returns carries a DigitalOcean payload.
"""

import os
from collections.abc import Mapping, Sequence
from typing import TypedDict, cast

import httpx

from gfz.hosts import ActionStatus, Host, HostStatus, PowerAction
from gfz.settings import TOKEN_VARIABLE

API_BASE_URL = "https://api.digitalocean.com/v2"
PER_PAGE = 200
REQUEST_TIMEOUT_SECONDS = 30.0


class DigitalOceanError(Exception):
    """The one error `gfz.digitalocean` raises.

    A failure the API reported carries the API's own error identifier in
    `error_id`, and the response's `request_id` when the response included
    one. A failure caught locally, such as a missing API token, carries
    neither: both attributes are `None`.

    Attributes:
        error_id: The DigitalOcean API's `id` for the failure, or `None` for
            a local failure or a response with none.
        request_id: The DigitalOcean API's `request_id` for the failure, or
            `None` for a local failure or a response with none.
    """

    def __init__(
        self,
        message: str,
        *,
        error_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        """Build the error with its message and, when known, its API identifiers."""
        super().__init__(message)
        self.error_id = error_id
        self.request_id = request_id


class _RegionPayload(TypedDict):
    """The nested region object a droplet payload carries."""

    slug: str


class _NetworkAddressPayload(TypedDict):
    """One address entry in a droplet payload's `networks.v4` list."""

    ip_address: str
    type: str


class _NetworksPayload(TypedDict):
    """The nested networks object a droplet payload carries."""

    v4: list[_NetworkAddressPayload]


class _DropletPayload(TypedDict):
    """The wire shape of one droplet, as the API returns it."""

    id: int
    name: str
    status: HostStatus
    region: _RegionPayload
    size_slug: str
    created_at: str
    tags: list[str]
    networks: _NetworksPayload


class _SshKeyPayload(TypedDict):
    """The wire shape of one account SSH key, as the API returns it."""

    id: int
    name: str
    fingerprint: str


class _ActionPayload(TypedDict):
    """The wire shape of one droplet action, as the API returns it."""

    status: ActionStatus


def _public_ipv4(networks: _NetworksPayload) -> str | None:
    """Return the `ip_address` of the `public` entry in `networks.v4`, or `None`.

    A droplet with no public entry — including a freshly created one, whose
    `v4` list is empty — has no address yet.
    """
    return next(
        (entry["ip_address"] for entry in networks["v4"] if entry["type"] == "public"),
        None,
    )


def _host_from_droplet(droplet: _DropletPayload) -> Host:
    """Map a droplet payload onto the provider-neutral `Host` record."""
    return Host(
        id=droplet["id"],
        name=droplet["name"],
        status=droplet["status"],
        region=droplet["region"]["slug"],
        size=droplet["size_slug"],
        ip=_public_ipv4(droplet["networks"]),
        created_at=droplet["created_at"],
        tags=tuple(droplet["tags"]),
    )


def _next_page_url(payload: dict[str, object]) -> str | None:
    """Return the pagination `links.pages.next` URL from a payload, or `None`.

    `links` may be absent, empty, or present without a `pages` key; all three
    end pagination rather than raising.
    """
    links = payload.get("links")
    if not isinstance(links, dict):
        return None
    pages = links.get("pages")
    if not isinstance(pages, dict):
        return None
    next_url = pages.get("next")
    return next_url if isinstance(next_url, str) else None


def _request_description(response: httpx.Response) -> str:
    """Return the method and URL of the request a response answers."""
    return f"{response.request.method} {response.request.url}"


def _json_object(response: httpx.Response) -> dict[str, object]:
    """Return the JSON object a successful response's body carries.

    Raises:
        DigitalOceanError: if the body is not JSON, or is JSON that is not
            an object. A success response that does not follow the
            documented shape has no local remedy either.
    """
    try:
        body = response.json()
    except ValueError as error:
        raise DigitalOceanError(
            f"DigitalOcean answered {_request_description(response)} "
            "with a body that is not JSON"
        ) from error
    if not isinstance(body, dict):
        raise DigitalOceanError(
            f"DigitalOcean answered {_request_description(response)} "
            "with a body that is not a JSON object"
        )
    return body


def _envelope(body: dict[str, object], key: str, response: httpx.Response) -> object:
    """Return the value a decoded body carries under its envelope `key`.

    Raises:
        DigitalOceanError: if the body carries no `key`. The message names
            the request that came back without it.
    """
    if key not in body:
        raise DigitalOceanError(
            f"DigitalOcean answered {_request_description(response)} "
            f"with no {key!r} in its body"
        )
    return body[key]


def _string_or_none(value: object) -> str | None:
    """Return `value` when it is a string, and `None` otherwise."""
    return value if isinstance(value, str) else None


def _error_fields(
    response: httpx.Response,
) -> tuple[str | None, str | None, str | None]:
    """Return the `message`, `id`, and `request_id` a failed response carries.

    Any of the three is `None` when the body omits it, when its value is not
    a string, or when the body is not JSON at all — a failed response is
    where a body that does not follow the documented shape is most likely.
    """
    try:
        body = response.json()
    except ValueError:
        return None, None, None
    if not isinstance(body, dict):
        return None, None, None
    return (
        _string_or_none(body.get("message")),
        _string_or_none(body.get("id")),
        _string_or_none(body.get("request_id")),
    )


def _error_from_response(
    response: httpx.Response, expected_status: int
) -> DigitalOceanError:
    """Build the error for a response whose status is not the expected one.

    The message always names the status the API returned, and carries the
    API's own wording whenever the body has any. A `401` states that the
    token was rejected and names the variable it comes from, since an
    expired token is the likely cause and the API's own wording does not say
    so. A `429` carries the rate limit's reset time. The token's value never
    appears in any of it.
    """
    api_message, error_id, request_id = _error_fields(response)
    if response.status_code == 401:
        message = f"DigitalOcean rejected the token in {TOKEN_VARIABLE} (401)"
    else:
        message = f"DigitalOcean API returned status {response.status_code}"
    detail = api_message or f"expected status {expected_status}"
    message = f"{message}: {detail}"
    if response.status_code == 429:
        reset = response.headers.get("ratelimit-reset", "unknown")
        message = f"{message}; the rate limit resets at {reset} Unix epoch seconds"
    if request_id is not None:
        message = f"{message} (request_id: {request_id})"
    return DigitalOceanError(message, error_id=error_id, request_id=request_id)


def _unknown_key_message(identifier: str, keys_by_name: Mapping[str, int]) -> str:
    """Return the error text for an identifier that matches no account SSH key."""
    if not keys_by_name:
        return f"no account SSH key named {identifier!r}; the account holds no SSH keys"
    known_names = ", ".join(sorted(keys_by_name))
    return f"no account SSH key named {identifier!r}; the account holds: {known_names}"


class DigitalOceanClient:
    """The DigitalOcean implementation of `gfz.hosts.HostClient`."""

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        """Build a client authenticated from the `DIGITALOCEAN_TOKEN` variable.

        `transport` is the seam a test injects an `httpx.MockTransport`
        through; `None` uses the real network transport.

        Raises:
            DigitalOceanError: if the environment variable named by
                `gfz.settings.TOKEN_VARIABLE` is unset or empty.
        """
        token = os.environ.get(TOKEN_VARIABLE, "")
        if not token:
            raise DigitalOceanError(
                f"{TOKEN_VARIABLE} is not set; the DigitalOcean API needs a token"
            )
        self._client = httpx.Client(
            base_url=API_BASE_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        expected_status: int,
    ) -> httpx.Response:
        """Send one request and return its response.

        Raises:
            DigitalOceanError: if the request never reaches the API, or if
                the response's status differs from `expected_status`. A
                connection that fails, times out, or breaks mid-response
                leaves this module as its own error, so a caller has one
                error type to catch.
        """
        try:
            response = self._client.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as error:
            raise DigitalOceanError(
                f"the request {method} {url} never reached DigitalOcean: {error}"
            ) from error
        if response.status_code != expected_status:
            raise _error_from_response(response, expected_status)
        return response

    def _collect_pages[Item](
        self, path: str, envelope: str, params: dict[str, str] | None = None
    ) -> list[Item]:
        """Return every page's items under `envelope`, following pagination to the end.

        Sends `GET path` with `params`, then follows `links.pages.next` — an
        absolute URL the API returns, sent with no extra params — until that
        key is absent.

        Raises:
            DigitalOceanError: if any page's request fails.
        """
        items: list[Item] = []
        url: str | None = path
        request_params: dict[str, str] | None = params
        while url is not None:
            response = self._request(
                "GET", url, params=request_params, expected_status=200
            )
            payload = _json_object(response)
            items.extend(cast(list[Item], _envelope(payload, envelope, response)))
            url = _next_page_url(payload)
            request_params = None
        return items

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
        """Create a droplet and return it as the API first reports it.

        The returned host has status `new`, and no address — an address
        appears only once the droplet becomes active. `ssh_keys` holds
        identifiers as the operator configured them; each is resolved to an
        id or a fingerprint before it is sent.

        Raises:
            DigitalOceanError: if the API call fails, including when an
                identifier in `ssh_keys` matches no account key.
        """
        body: dict[str, object] = {
            "name": name,
            "region": region,
            "size": size,
            "image": image,
            "ssh_keys": self._resolve_ssh_keys(ssh_keys),
            "tags": list(tags),
            "monitoring": True,
            "backups": False,
            "ipv6": False,
        }
        response = self._request(
            "POST", "/droplets", json_body=body, expected_status=202
        )
        droplet = _envelope(_json_object(response), "droplet", response)
        return _host_from_droplet(cast(_DropletPayload, droplet))

    def _account_keys(self) -> list[_SshKeyPayload]:
        """Return every SSH key the account holds, following pagination.

        Raises:
            DigitalOceanError: if the API call fails.
        """
        return self._collect_pages(
            "/account/keys", "ssh_keys", params={"per_page": str(PER_PAGE)}
        )

    def _resolve_ssh_keys(self, identifiers: Sequence[str]) -> list[int | str]:
        """Resolve each configured SSH key identifier to an id or a fingerprint.

        `ssh_keys` on the create call accepts only numeric ids and
        fingerprints, not names, so a name is resolved here. An identifier
        that is all digits becomes its `int` value; one containing a colon
        is a fingerprint and passes through unchanged; anything else is a
        name, looked up against the account's SSH keys. The account-keys
        endpoint is called only when at least one identifier is a name.
        Order is preserved.

        Raises:
            DigitalOceanError: if a name identifier matches no account key.
        """
        needs_lookup = any(
            not identifier.isdigit() and ":" not in identifier
            for identifier in identifiers
        )
        keys_by_name: dict[str, int] = {}
        if needs_lookup:
            keys_by_name = {key["name"]: key["id"] for key in self._account_keys()}

        resolved: list[int | str] = []
        for identifier in identifiers:
            if identifier.isdigit():
                resolved.append(int(identifier))
            elif ":" in identifier:
                resolved.append(identifier)
            elif identifier in keys_by_name:
                resolved.append(keys_by_name[identifier])
            else:
                raise DigitalOceanError(_unknown_key_message(identifier, keys_by_name))
        return resolved

    def get_host(self, host_id: int) -> Host:
        """Return the droplet with this id as a `Host`.

        Raises:
            DigitalOceanError: if the API call fails, including when no
                droplet has this id.
        """
        response = self._request("GET", f"/droplets/{host_id}", expected_status=200)
        droplet = _envelope(_json_object(response), "droplet", response)
        return _host_from_droplet(cast(_DropletPayload, droplet))

    def list_hosts(self, *, tag: str | None = None) -> list[Host]:
        """Return every droplet, or only those carrying `tag`, as `Host` values.

        Raises:
            DigitalOceanError: if the API call fails.
        """
        params = {"per_page": str(PER_PAGE)}
        if tag is not None:
            params["tag_name"] = tag
        droplets: list[_DropletPayload] = self._collect_pages(
            "/droplets", "droplets", params=params
        )
        return [_host_from_droplet(droplet) for droplet in droplets]

    def delete_host(self, host_id: int) -> None:
        """Delete the droplet with this id.

        Raises:
            DigitalOceanError: if the API call fails.
        """
        self._request("DELETE", f"/droplets/{host_id}", expected_status=204)

    def power_host(self, host_id: int, action: PowerAction) -> ActionStatus:
        """Run a power action on the droplet with this id and return its status.

        `power_on` starts the droplet, `shutdown` stops it gracefully, and
        `power_off` cuts power immediately; the caller chooses which.

        Raises:
            DigitalOceanError: if the API call fails.
        """
        response = self._request(
            "POST",
            f"/droplets/{host_id}/actions",
            json_body={"type": action},
            expected_status=201,
        )
        performed = _envelope(_json_object(response), "action", response)
        return cast(_ActionPayload, performed)["status"]

    def list_account_keys(self) -> list[str]:
        """Return the name of every SSH key the account holds.

        Raises:
            DigitalOceanError: if the API call fails.
        """
        return [key["name"] for key in self._account_keys()]

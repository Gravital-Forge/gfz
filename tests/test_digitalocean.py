"""Tests for gfz's DigitalOcean provider client.

Every request assertion goes through `httpx.MockTransport`; no test reaches
the network.
"""

import inspect
import json
from collections.abc import Callable

import httpx
import pytest

from gfz.digitalocean import (
    API_BASE_URL,
    PER_PAGE,
    DigitalOceanClient,
    DigitalOceanError,
)
from gfz.hosts import HostClient

TEST_TOKEN = "canned-test-token"  # noqa: S105 — a fixture value, not a real credential


@pytest.fixture(autouse=True)
def _digitalocean_token(monkeypatch, _isolated_environment):
    """Set DIGITALOCEAN_TOKEN for every test in this module.

    Depends on `_isolated_environment` so the clearing conftest fixture runs
    first and this one's value is the one tests see.
    """
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", TEST_TOKEN)


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> DigitalOceanClient:
    """Build a DigitalOceanClient whose requests go through a MockTransport handler."""
    return DigitalOceanClient(transport=httpx.MockTransport(handler))


def _recording_handler(
    response_for: Callable[[httpx.Request], httpx.Response],
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    """Wrap `response_for` in a handler that also records every request it sees."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return response_for(request)

    return handler, requests


def _droplet_payload(**overrides: object) -> dict[str, object]:
    """Build a canned droplet payload, overridden field by field."""
    payload: dict[str, object] = {
        "id": 123,
        "name": "agent-host-1",
        "status": "active",
        "region": {"slug": "tor1"},
        "size_slug": "s-4vcpu-8gb",
        "created_at": "2026-01-01T00:00:00Z",
        "tags": ["gfz"],
        "networks": {
            "v4": [
                {"ip_address": "203.0.113.5", "type": "public"},
                {"ip_address": "10.0.0.5", "type": "private"},
            ]
        },
    }
    payload.update(overrides)
    return payload


def test_get_host_sends_get_to_droplet_path_with_bearer_header():
    """get_host sends GET /v2/droplets/{id} with the bearer authorization header."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(200, json={"droplet": _droplet_payload()})
    )
    client = _client(handler)

    client.get_host(123)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v2/droplets/123"
    assert request.headers["Authorization"] == f"Bearer {TEST_TOKEN}"


def test_get_host_decodes_every_field_onto_host():
    """get_host maps a canned droplet payload onto every Host field."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(200, json={"droplet": _droplet_payload()})
    )
    client = _client(handler)

    host = client.get_host(123)

    assert host.id == 123
    assert host.name == "agent-host-1"
    assert host.status == "active"
    assert host.region == "tor1"
    assert host.size == "s-4vcpu-8gb"
    assert host.ip == "203.0.113.5"
    assert host.created_at == "2026-01-01T00:00:00Z"
    assert host.tags == ("gfz",)


def test_get_host_with_only_a_private_address_has_no_ip():
    """A droplet whose networks.v4 holds only a private entry maps to ip None."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(
            200,
            json={
                "droplet": _droplet_payload(
                    networks={"v4": [{"ip_address": "10.0.0.5", "type": "private"}]}
                )
            },
        )
    )
    client = _client(handler)

    host = client.get_host(123)

    assert host.ip is None


def test_get_host_freshly_created_with_empty_v4_has_no_ip():
    """A newly created droplet, status new with an empty v4 list, maps to ip None."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(
            200, json={"droplet": _droplet_payload(status="new", networks={"v4": []})}
        )
    )
    client = _client(handler)

    host = client.get_host(123)

    assert host.status == "new"
    assert host.ip is None


def test_list_hosts_without_tag_sends_per_page_and_no_tag_name():
    """list_hosts() sends GET /v2/droplets with per_page=200 and no tag_name."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(200, json={"droplets": [_droplet_payload()]})
    )
    client = _client(handler)

    client.list_hosts()

    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v2/droplets"
    assert request.url.params["per_page"] == str(PER_PAGE)
    assert "tag_name" not in request.url.params


def test_list_hosts_with_tag_sends_tag_name_alongside_per_page():
    """list_hosts(tag=...) sends tag_name alongside per_page=200."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(200, json={"droplets": []})
    )
    client = _client(handler)

    client.list_hosts(tag="gfz")

    request = requests[0]
    assert request.url.params["tag_name"] == "gfz"
    assert request.url.params["per_page"] == str(PER_PAGE)


def test_list_hosts_follows_pagination_to_completion():
    """Two pages of droplets, linked by links.pages.next, become one ordered list."""
    first_droplet = _droplet_payload(id=1, name="host-1")
    second_droplet = _droplet_payload(id=2, name="host-2")
    next_url = f"{API_BASE_URL}/droplets?per_page={PER_PAGE}&page=2"

    def response_for(request: httpx.Request) -> httpx.Response:
        if str(request.url) == next_url:
            return httpx.Response(200, json={"droplets": [second_droplet]})
        return httpx.Response(
            200,
            json={
                "droplets": [first_droplet],
                "links": {"pages": {"next": next_url}},
            },
        )

    handler, requests = _recording_handler(response_for)
    client = _client(handler)

    hosts = client.list_hosts()

    assert [host.id for host in hosts] == [1, 2]
    assert len(requests) == 2
    assert str(requests[0].url) == f"{API_BASE_URL}/droplets?per_page={PER_PAGE}"
    assert str(requests[1].url) == next_url


def test_list_hosts_with_links_absent_ends_pagination():
    """A response with no links key ends pagination without raising."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(200, json={"droplets": [_droplet_payload()]})
    )
    client = _client(handler)

    hosts = client.list_hosts()

    assert len(hosts) == 1
    assert len(requests) == 1


def test_list_hosts_with_empty_links_ends_pagination():
    """A response with an empty links object ends pagination without raising."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(
            200, json={"droplets": [_droplet_payload()], "links": {}}
        )
    )
    client = _client(handler)

    hosts = client.list_hosts()

    assert len(hosts) == 1
    assert len(requests) == 1


def test_delete_host_sends_delete_and_returns_none():
    """delete_host sends DELETE /v2/droplets/{id}, accepts 204, and returns None."""
    handler, requests = _recording_handler(lambda request: httpx.Response(204))
    client = _client(handler)

    result = client.delete_host(123)

    assert result is None
    request = requests[0]
    assert request.method == "DELETE"
    assert request.url.path == "/v2/droplets/123"


def test_api_error_response_carries_id_message_and_request_id():
    """A 404 error body's id, message, and request_id land on the raised error."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(
            404,
            json={
                "id": "not_found",
                "message": "The resource you requested could not be found.",
                "request_id": "req-123",
            },
        )
    )
    client = _client(handler)

    with pytest.raises(DigitalOceanError) as exc_info:
        client.get_host(123)

    error = exc_info.value
    assert error.error_id == "not_found"
    assert error.request_id == "req-123"
    assert "could not be found" in str(error)


def test_api_error_message_names_the_status_and_the_request_id():
    """An error message names the API's status and request id beside its wording."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(
            404,
            json={
                "id": "not_found",
                "message": "Droplet not found.",
                "request_id": "req-456",
            },
        )
    )
    client = _client(handler)

    with pytest.raises(DigitalOceanError) as exc_info:
        client.get_host(123)

    message = str(exc_info.value)
    assert "404" in message
    assert "Droplet not found." in message
    assert "req-456" in message


def test_401_response_names_the_token_variable_and_hides_its_value():
    """A 401 response's message says the token was rejected and names the variable."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(
            401, json={"id": "unauthorized", "message": "Unable to authenticate you."}
        )
    )
    client = _client(handler)

    with pytest.raises(DigitalOceanError) as exc_info:
        client.get_host(123)

    message = str(exc_info.value)
    assert "token" in message.lower()
    assert "DIGITALOCEAN_TOKEN" in message
    assert TEST_TOKEN not in message


def test_429_response_includes_the_ratelimit_reset_header_value():
    """A 429 response's message includes the ratelimit-reset header's value."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(
            429,
            json={"id": "too_many_requests", "message": "API Rate limit exceeded."},
            headers={"ratelimit-reset": "1735689600"},
        )
    )
    client = _client(handler)

    with pytest.raises(DigitalOceanError) as exc_info:
        client.get_host(123)

    assert "1735689600" in str(exc_info.value)


def test_unexpected_status_with_non_json_body_raises_domain_error():
    """A non-JSON error body raises DigitalOceanError, not a JSON decode error."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(500, text="internal server error")
    )
    client = _client(handler)

    with pytest.raises(DigitalOceanError) as exc_info:
        client.get_host(123)

    message = str(exc_info.value)
    assert "500" in message
    assert "200" in message


def test_missing_token_raises_naming_the_variable(monkeypatch):
    """Constructing the client with DIGITALOCEAN_TOKEN unset names the variable."""
    monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)

    with pytest.raises(DigitalOceanError, match="DIGITALOCEAN_TOKEN"):
        DigitalOceanClient()


def test_empty_token_raises_naming_the_variable(monkeypatch):
    """Constructing the client with DIGITALOCEAN_TOKEN set empty names the variable."""
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "")

    with pytest.raises(DigitalOceanError, match="DIGITALOCEAN_TOKEN"):
        DigitalOceanClient()


def _new_droplet_payload(**overrides: object) -> dict[str, object]:
    """Build a canned droplet payload shaped as a freshly created droplet."""
    return _droplet_payload(status="new", networks={"v4": []}, **overrides)


def _ssh_key_payload(*, id: int, name: str, fingerprint: str = "") -> dict[str, object]:
    """Build a canned account SSH key payload."""
    return {"id": id, "name": name, "fingerprint": fingerprint}


def _request_body(request: httpx.Request) -> dict[str, object]:
    """Decode a recorded MockTransport request's JSON body."""
    return json.loads(request.content)


def test_create_host_sends_post_with_exact_body_and_no_user_data():
    """create_host POSTs /v2/droplets with exactly the expected body, no user_data."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(202, json={"droplet": _new_droplet_payload()})
    )
    client = _client(handler)

    client.create_host(
        name="agent-host-1",
        region="tor1",
        size="s-4vcpu-8gb",
        image="ubuntu-24-04-x64",
        ssh_keys=["12345", "aa:bb:cc:dd:ee:ff"],
        tags=["gfz"],
    )

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v2/droplets"
    body = _request_body(request)
    assert body == {
        "name": "agent-host-1",
        "region": "tor1",
        "size": "s-4vcpu-8gb",
        "image": "ubuntu-24-04-x64",
        "ssh_keys": [12345, "aa:bb:cc:dd:ee:ff"],
        "tags": ["gfz"],
        "monitoring": True,
        "backups": False,
        "ipv6": False,
    }
    assert "user_data" not in body


def test_create_host_decodes_freshly_created_droplet_with_no_ip():
    """create_host decodes the 202 droplet envelope: status new, ip None."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(202, json={"droplet": _new_droplet_payload()})
    )
    client = _client(handler)

    host = client.create_host(
        name="agent-host-1",
        region="tor1",
        size="s-4vcpu-8gb",
        image="ubuntu-24-04-x64",
        ssh_keys=["12345"],
        tags=["gfz"],
    )

    assert host.status == "new"
    assert host.ip is None


def test_create_host_with_ids_and_fingerprints_makes_no_account_keys_request():
    """All-digit ids and colon-bearing fingerprints send no GET /v2/account/keys."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(202, json={"droplet": _new_droplet_payload()})
    )
    client = _client(handler)

    client.create_host(
        name="agent-host-1",
        region="tor1",
        size="s-4vcpu-8gb",
        image="ubuntu-24-04-x64",
        ssh_keys=["12345", "aa:bb:cc:dd:ee:ff"],
        tags=["gfz"],
    )

    assert len(requests) == 1
    assert requests[0].method == "POST"
    body = _request_body(requests[0])
    assert body["ssh_keys"] == [12345, "aa:bb:cc:dd:ee:ff"]


def test_create_host_resolves_a_name_via_account_keys_before_posting():
    """A name fetches GET /v2/account/keys with per_page=200 before the POST."""

    def response_for(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/account/keys":
            return httpx.Response(
                200,
                json={"ssh_keys": [_ssh_key_payload(id=987, name="laptop")]},
            )
        return httpx.Response(202, json={"droplet": _new_droplet_payload()})

    handler, requests = _recording_handler(response_for)
    client = _client(handler)

    client.create_host(
        name="agent-host-1",
        region="tor1",
        size="s-4vcpu-8gb",
        image="ubuntu-24-04-x64",
        ssh_keys=["laptop"],
        tags=["gfz"],
    )

    assert len(requests) == 2
    keys_request, create_request = requests
    assert keys_request.method == "GET"
    assert keys_request.url.path == "/v2/account/keys"
    assert keys_request.url.params["per_page"] == str(PER_PAGE)
    assert create_request.method == "POST"
    assert _request_body(create_request)["ssh_keys"] == [987]


def test_create_host_resolution_keeps_identifier_order_across_kinds():
    """A mix of a name, an id, and a fingerprint keeps the identifiers' order."""

    def response_for(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/account/keys":
            return httpx.Response(
                200,
                json={"ssh_keys": [_ssh_key_payload(id=987, name="laptop")]},
            )
        return httpx.Response(202, json={"droplet": _new_droplet_payload()})

    handler, requests = _recording_handler(response_for)
    client = _client(handler)

    client.create_host(
        name="agent-host-1",
        region="tor1",
        size="s-4vcpu-8gb",
        image="ubuntu-24-04-x64",
        ssh_keys=["laptop", "12345", "aa:bb:cc:dd:ee:ff"],
        tags=["gfz"],
    )

    create_request = requests[-1]
    resolved = _request_body(create_request)["ssh_keys"]
    assert resolved == [987, 12345, "aa:bb:cc:dd:ee:ff"]


def test_create_host_unknown_name_raises_and_sends_no_create_request():
    """An unknown name names itself and every account key name, and sends no POST."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(
            200,
            json={
                "ssh_keys": [
                    _ssh_key_payload(id=1, name="alpha"),
                    _ssh_key_payload(id=2, name="bravo"),
                ]
            },
        )
    )
    client = _client(handler)

    with pytest.raises(DigitalOceanError) as exc_info:
        client.create_host(
            name="agent-host-1",
            region="tor1",
            size="s-4vcpu-8gb",
            image="ubuntu-24-04-x64",
            ssh_keys=["nonexistent"],
            tags=["gfz"],
        )

    message = str(exc_info.value)
    assert "nonexistent" in message
    assert "alpha" in message
    assert "bravo" in message
    assert all(request.method != "POST" for request in requests)


def test_create_host_unknown_name_with_no_account_keys_says_account_holds_none():
    """An unknown name against an account with no keys says the account holds none."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(200, json={"ssh_keys": []})
    )
    client = _client(handler)

    with pytest.raises(DigitalOceanError) as exc_info:
        client.create_host(
            name="agent-host-1",
            region="tor1",
            size="s-4vcpu-8gb",
            image="ubuntu-24-04-x64",
            ssh_keys=["nonexistent"],
            tags=["gfz"],
        )

    message = str(exc_info.value).lower()
    assert "nonexistent" in message
    assert "no" in message


def test_list_account_keys_sends_get_with_per_page_and_returns_names_in_order():
    """list_account_keys sends GET /v2/account/keys, per_page=200, names in order."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(
            200,
            json={
                "ssh_keys": [
                    _ssh_key_payload(id=1, name="alpha"),
                    _ssh_key_payload(id=2, name="bravo"),
                ]
            },
        )
    )
    client = _client(handler)

    names = client.list_account_keys()

    assert names == ["alpha", "bravo"]
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "GET"
    assert request.url.path == "/v2/account/keys"
    assert request.url.params["per_page"] == str(PER_PAGE)


def test_list_account_keys_follows_pagination_across_two_pages():
    """list_account_keys follows links.pages.next across two pages of ssh_keys."""
    next_url = f"{API_BASE_URL}/account/keys?per_page={PER_PAGE}&page=2"

    def response_for(request: httpx.Request) -> httpx.Response:
        if str(request.url) == next_url:
            return httpx.Response(
                200, json={"ssh_keys": [_ssh_key_payload(id=2, name="bravo")]}
            )
        return httpx.Response(
            200,
            json={
                "ssh_keys": [_ssh_key_payload(id=1, name="alpha")],
                "links": {"pages": {"next": next_url}},
            },
        )

    handler, requests = _recording_handler(response_for)
    client = _client(handler)

    names = client.list_account_keys()

    assert names == ["alpha", "bravo"]
    assert len(requests) == 2


@pytest.mark.parametrize("action", ["power_on", "shutdown", "power_off"])
def test_power_host_sends_post_action_body_and_returns_status(action):
    """power_host POSTs the action body to /v2/droplets/{id}/actions, returns status."""
    handler, requests = _recording_handler(
        lambda request: httpx.Response(201, json={"action": {"status": "in-progress"}})
    )
    client = _client(handler)

    status = client.power_host(123, action)

    assert status == "in-progress"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v2/droplets/123/actions"
    assert _request_body(request) == {"type": action}


def test_power_host_reports_errored_status_without_raising():
    """A 201 action reporting errored returns "errored" rather than raising."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(201, json={"action": {"status": "errored"}})
    )
    client = _client(handler)

    status = client.power_host(123, "power_on")

    assert status == "errored"


def test_digitalocean_client_answers_every_hostclient_operation():
    """DigitalOceanClient exposes every public operation HostClient declares."""
    operation_names = [name for name in vars(HostClient) if not name.startswith("_")]

    assert operation_names

    client = _client(lambda request: httpx.Response(200, json={}))
    for name in operation_names:
        assert callable(getattr(client, name))


def test_digitalocean_client_takes_the_parameters_the_protocol_declares():
    """Every operation's parameters match the protocol's, name for name."""
    operation_names = [name for name in vars(HostClient) if not name.startswith("_")]

    for name in operation_names:
        declared = inspect.signature(getattr(HostClient, name)).parameters
        implemented = inspect.signature(getattr(DigitalOceanClient, name)).parameters
        assert list(implemented.items()) == list(declared.items())


def test_a_request_that_never_reaches_the_api_raises_the_module_error():
    """A transport failure leaves this module as DigitalOceanError, not an httpx one."""

    def response_for(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(response_for)

    with pytest.raises(DigitalOceanError) as exc_info:
        client.get_host(123)

    error = exc_info.value
    assert "/droplets/123" in str(error)
    assert isinstance(error.__cause__, httpx.ConnectError)


def test_success_body_that_is_not_json_raises_the_module_error():
    """A success response whose body is not JSON raises DigitalOceanError."""
    handler, _ = _recording_handler(
        lambda request: httpx.Response(200, text="<html>not json</html>")
    )
    client = _client(handler)

    with pytest.raises(DigitalOceanError, match="not JSON"):
        client.get_host(123)


def test_success_body_without_its_envelope_raises_the_module_error():
    """A success response missing its envelope key raises DigitalOceanError."""
    handler, _ = _recording_handler(lambda request: httpx.Response(200, json={}))
    client = _client(handler)

    with pytest.raises(DigitalOceanError, match="droplet"):
        client.get_host(123)

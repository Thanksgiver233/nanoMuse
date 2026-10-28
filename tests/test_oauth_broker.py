"""The OAuth broker, against a mock vendor: authorize, refresh, revoke, disconnect.

No HTTP, no real vendor, no real account: the vendor below is a script that
answers the way a token endpoint would. What is asserted throughout is the
broker's promise — a token value never reaches the caller except through
``token_for``, and disconnecting leaves nothing behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanomuse.oauth_broker import (
    OAuthBroker,
    OAuthBrokerError,
    TokenSet,
    placeholder,
    provider_of,
)
from nanomuse.vault import CredentialVault

ACCESS = "mock-access-token-value-1"
REFRESHED = "mock-access-token-value-2"
REFRESH = "mock-refresh-token-value-1"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class MockVendor:
    """A vendor that issues one token per exchange and one per refresh."""

    def __init__(self) -> None:
        self.revoked: list[str] = []
        self.refresh_calls = 0
        self.fail_refresh = False

    def authorize_url(self, state: str, scopes: tuple[str, ...]) -> str:
        scope = "+".join(scopes)
        return f"https://vendor.example/authorize?state={state}&scope={scope}"

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        assert code == "auth-code-1"
        return TokenSet(
            access_token=ACCESS,
            refresh_token=REFRESH,
            expires_in=3600,
            scopes=("read", "write"),
        )

    async def refresh(self, refresh_token: str) -> TokenSet:
        assert refresh_token == REFRESH
        self.refresh_calls += 1
        if self.fail_refresh:
            raise RuntimeError("vendor refused the refresh")
        return TokenSet(
            access_token=REFRESHED,
            refresh_token=REFRESH,
            expires_in=3600,
            scopes=("read", "write"),
        )

    async def revoke(self, token: str) -> None:
        self.revoked.append(token)


@pytest.fixture
def broker(tmp_path: Path) -> tuple[OAuthBroker, MockVendor, FakeClock]:
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    vendor = MockVendor()
    clock = FakeClock()
    return OAuthBroker(vault, {"mockvendor": vendor}, clock), vendor, clock


async def test_connect_stores_tokens_and_reports_no_values(
    broker: tuple[OAuthBroker, MockVendor, FakeClock],
) -> None:
    br, _, _ = broker
    status = await br.connect("mockvendor", "auth-code-1")
    assert status.connected
    assert status.scopes == ("read", "write")
    assert status.expires_in == 3600
    assert status.has_refresh_token
    assert ACCESS not in repr(br)
    assert ACCESS not in str(status)


async def test_placeholder_names_the_provider_and_nothing_else() -> None:
    assert placeholder("mockvendor") == "{{oauth:mockvendor}}"
    assert provider_of("{{oauth:mockvendor}}") == "mockvendor"
    assert provider_of("{{vault:EMAIL}}") is None
    assert provider_of("plain text") is None


async def test_token_for_returns_the_live_token_before_expiry(
    broker: tuple[OAuthBroker, MockVendor, FakeClock],
) -> None:
    br, vendor, _ = broker
    await br.connect("mockvendor", "auth-code-1")
    assert await br.token_for("mockvendor") == ACCESS
    assert vendor.refresh_calls == 0


async def test_token_for_refreshes_once_expired(
    broker: tuple[OAuthBroker, MockVendor, FakeClock],
) -> None:
    br, vendor, clock = broker
    await br.connect("mockvendor", "auth-code-1")
    clock.now += 3601
    assert await br.token_for("mockvendor") == REFRESHED
    assert vendor.refresh_calls == 1
    assert await br.token_for("mockvendor") == REFRESHED
    assert vendor.refresh_calls == 1  # the refreshed token is used, not refreshed again


async def test_token_for_refuses_scopes_beyond_the_grant(
    broker: tuple[OAuthBroker, MockVendor, FakeClock],
) -> None:
    br, _, _ = broker
    await br.connect("mockvendor", "auth-code-1")
    with pytest.raises(OAuthBrokerError, match="scopes"):
        await br.token_for("mockvendor", ("admin",))


async def test_failed_refresh_disconnects(
    broker: tuple[OAuthBroker, MockVendor, FakeClock],
) -> None:
    br, vendor, clock = broker
    await br.connect("mockvendor", "auth-code-1")
    vendor.fail_refresh = True
    clock.now += 3601
    with pytest.raises(OAuthBrokerError, match="disconnected"):
        await br.token_for("mockvendor")
    assert not br.is_connected("mockvendor")
    assert ACCESS in vendor.revoked


async def test_disconnect_revokes_and_leaves_no_residue(
    broker: tuple[OAuthBroker, MockVendor, FakeClock], tmp_path: Path
) -> None:
    br, vendor, _ = broker
    vault = CredentialVault(tmp_path / "vault.enc", tmp_path / "vault.key")
    await br.connect("mockvendor", "auth-code-1")
    assert await br.disconnect("mockvendor") is True
    assert vendor.revoked == [ACCESS, REFRESH]
    assert vault.names() == []
    assert not br.is_connected("mockvendor")
    with pytest.raises(OAuthBrokerError, match="not connected"):
        await br.token_for("mockvendor")


async def test_disconnect_of_an_unconnected_provider_is_quiet(
    broker: tuple[OAuthBroker, MockVendor, FakeClock],
) -> None:
    br, _, _ = broker
    assert await br.disconnect("mockvendor") is False


async def test_unknown_provider_is_an_error_not_a_token(
    broker: tuple[OAuthBroker, MockVendor, FakeClock],
) -> None:
    br, _, _ = broker
    with pytest.raises(OAuthBrokerError, match="no vendor"):
        await br.connect("elsewhere", "code")
    with pytest.raises(OAuthBrokerError, match="no vendor"):
        await br.token_for("elsewhere")


async def test_scrub_replaces_stored_tokens_with_placeholders(
    broker: tuple[OAuthBroker, MockVendor, FakeClock],
) -> None:
    br, _, _ = broker
    await br.connect("mockvendor", "auth-code-1")
    text = f"called with Authorization: Bearer {ACCESS} and refresh {REFRESH}"
    scrubbed = br.scrub(text)
    assert ACCESS not in scrubbed and REFRESH not in scrubbed
    assert scrubbed.count("{{oauth:mockvendor}}") == 2
    assert br.scrub("nothing secret here") == "nothing secret here"


async def test_authorize_url_comes_from_the_vendor(
    broker: tuple[OAuthBroker, MockVendor, FakeClock],
) -> None:
    br, _, _ = broker
    url = br.authorize_url("mockvendor", "state-9", ("read",))
    assert "state-9" in url and "read" in url

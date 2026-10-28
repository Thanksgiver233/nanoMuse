"""OAuth broker: tokens live here, the model only ever sees a placeholder.

A connected account's tokens are held in the vault (encrypted on disk) under
``oauth.<provider>.*`` names and handed out only to connector code that is about
to make the call, through :meth:`OAuthBroker.token_for`. Everything the model
sees — prompts, tool arguments, the audit log, traces — carries
``{{oauth:<provider>}}`` and nothing else; :meth:`OAuthBroker.scrub` is the
second line of defence for text that already left the broker's hands.
Disconnecting revokes at the vendor first and then deletes the vault entries,
so a disconnected account leaves no token behind to be found later.

The vendor itself is a :class:`OAuthVendor`: the broker never speaks HTTP, and
no real vendor is contacted anywhere in this module. Tests use a mock vendor;
a real one is a small adapter over the vendor's token endpoint.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Protocol

from nanomuse.vault import CredentialVault

PLACEHOLDER_PREFIX = "{{oauth:"
PLACEHOLDER_SUFFIX = "}}"


class OAuthBrokerError(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenSet:
    """What a vendor hands back for an authorization code or a refresh token."""

    access_token: str
    refresh_token: str | None
    expires_in: int  # seconds until the access token stops working
    scopes: tuple[str, ...] = ()
    token_type: str = "Bearer"


class OAuthVendor(Protocol):
    """The vendor's token endpoint, behind whatever transport the adapter uses."""

    def authorize_url(self, state: str, scopes: tuple[str, ...]) -> str:
        """Where the person is sent to approve access (the broker never opens it)."""
        ...

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenSet:
        """Trade the authorization code from the vendor's redirect for tokens."""
        ...

    async def refresh(self, refresh_token: str) -> TokenSet:
        """Trade a refresh token for a new access token."""
        ...

    async def revoke(self, token: str) -> None:
        """Tell the vendor a token is dead. Best effort: the broker deletes locally
        whatever the vendor answers."""
        ...


def placeholder(provider: str) -> str:
    """The string the model sees for a connected account. Never a token."""
    return f"{PLACEHOLDER_PREFIX}{provider}{PLACEHOLDER_SUFFIX}"


def provider_of(value: str) -> str | None:
    """The provider a placeholder names, or None when the string is not one."""
    if value.startswith(PLACEHOLDER_PREFIX) and value.endswith(PLACEHOLDER_SUFFIX):
        name = value[len(PLACEHOLDER_PREFIX) : -len(PLACEHOLDER_SUFFIX)]
        return name or None
    return None


@dataclass(frozen=True)
class ConnectionStatus:
    provider: str
    connected: bool
    scopes: tuple[str, ...] = ()
    expires_in: int | None = None  # seconds left, None when unknown or disconnected
    has_refresh_token: bool = False


class OAuthBroker:
    """Token storage and refresh for connected accounts, behind placeholders."""

    def __init__(
        self,
        vault: CredentialVault,
        vendors: dict[str, OAuthVendor],
        clock: object = time.time,  # noqa: ANN001 - a time.time-shaped callable, for tests
    ):
        self._vault = vault
        self._vendors = dict(vendors)
        self._clock = clock  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"OAuthBroker(providers={sorted(self._vendors)})"

    # ------------------------------------------------------------------ names
    @staticmethod
    def _access_key(provider: str) -> str:
        return f"oauth.{provider}.access"

    @staticmethod
    def _refresh_key(provider: str) -> str:
        return f"oauth.{provider}.refresh"

    @staticmethod
    def _meta_key(provider: str) -> str:
        return f"oauth.{provider}.meta"

    def _meta(self, provider: str) -> dict[str, object]:
        raw = self._vault.get(self._meta_key(provider))
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_meta(self, provider: str, meta: dict[str, object]) -> None:
        self._vault.set(self._meta_key(provider), json.dumps(meta, ensure_ascii=False))

    # ------------------------------------------------------------------ connect
    def authorize_url(self, provider: str, state: str, scopes: tuple[str, ...] = ()) -> str:
        vendor = self._vendors.get(provider)
        if vendor is None:
            raise OAuthBrokerError(f"no vendor registered for '{provider}'")
        return vendor.authorize_url(state, scopes)

    async def connect(self, provider: str, code: str, redirect_uri: str = "") -> ConnectionStatus:
        """Finish an authorization: exchange the code, store the tokens, report
        the connection without ever returning a token value."""
        vendor = self._vendors.get(provider)
        if vendor is None:
            raise OAuthBrokerError(f"no vendor registered for '{provider}'")
        tokens = await vendor.exchange_code(code, redirect_uri)
        self._store(provider, tokens)
        return self.status(provider)

    def _store(self, provider: str, tokens: TokenSet) -> None:
        self._vault.set(self._access_key(provider), tokens.access_token)
        if tokens.refresh_token:
            self._vault.set(self._refresh_key(provider), tokens.refresh_token)
        else:
            self._vault.delete(self._refresh_key(provider))
        self._save_meta(
            provider,
            {
                "scopes": list(tokens.scopes),
                "expires_at": self._clock() + max(tokens.expires_in, 0),
                "token_type": tokens.token_type,
            },
        )

    # ------------------------------------------------------------------ status
    def status(self, provider: str) -> ConnectionStatus:
        meta = self._meta(provider)
        access = self._vault.get(self._access_key(provider))
        if access is None or not meta:
            return ConnectionStatus(provider=provider, connected=False)
        scopes = tuple(str(s) for s in meta.get("scopes", []) if isinstance(s, str))
        expires_at = meta.get("expires_at")
        left = None
        if isinstance(expires_at, (int, float)):
            left = max(0, int(expires_at - self._clock()))
        return ConnectionStatus(
            provider=provider,
            connected=True,
            scopes=scopes,
            expires_in=left,
            has_refresh_token=self._vault.get(self._refresh_key(provider)) is not None,
        )

    def is_connected(self, provider: str) -> bool:
        return self.status(provider).connected

    # ------------------------------------------------------------------ use
    async def token_for(self, provider: str, scopes: tuple[str, ...] = ()) -> str:
        """The live access token for one call. Refreshes when it has expired; when
        the refresh fails the connection is dropped and the caller hears that the
        account is disconnected, not a stale token."""
        vendor = self._vendors.get(provider)
        if vendor is None:
            raise OAuthBrokerError(f"no vendor registered for '{provider}'")
        status = self.status(provider)
        if not status.connected:
            raise OAuthBrokerError(f"'{provider}' is not connected")
        if scopes and not set(scopes).issubset(status.scopes):
            raise OAuthBrokerError(f"'{provider}' was connected without the scopes this call needs")
        if status.expires_in == 0:
            refresh_token = self._vault.get(self._refresh_key(provider))
            if refresh_token is None:
                await self.disconnect(provider)
                raise OAuthBrokerError(f"'{provider}' expired and has no refresh token")
            try:
                tokens = await vendor.refresh(refresh_token)
            except Exception as exc:  # noqa: BLE001 - any vendor failure drops the connection
                await self.disconnect(provider)
                raise OAuthBrokerError(
                    f"'{provider}' could not be refreshed; the account is disconnected"
                ) from exc
            self._store(provider, tokens)
        token = self._vault.get(self._access_key(provider))
        if token is None:  # pragma: no cover - status said connected a moment ago
            raise OAuthBrokerError(f"'{provider}' is not connected")
        return token

    # ------------------------------------------------------------------ disconnect
    async def disconnect(self, provider: str) -> bool:
        """Revoke at the vendor, then delete every stored value. Returns False when
        nothing was connected; a vendor that refuses the revoke still loses the
        local copies, so the tokens cannot be used again from here."""
        vendor = self._vendors.get(provider)
        access = self._vault.get(self._access_key(provider))
        refresh = self._vault.get(self._refresh_key(provider))
        connected = access is not None
        if vendor is not None:
            for token in (access, refresh):
                if token:
                    try:
                        await vendor.revoke(token)
                    except Exception:  # noqa: BLE001 - the local delete below still runs
                        pass
        self._vault.delete(self._access_key(provider))
        self._vault.delete(self._refresh_key(provider))
        self._vault.delete(self._meta_key(provider))
        return connected

    # ------------------------------------------------------------------ scrubbing
    def scrub(self, text: str) -> str:
        """Replace any stored token value appearing in ``text`` with its placeholder.

        The broker knows every value it holds; a value that was never stored here
        is not this method's business (the vault's own ``redact`` covers those)."""
        if not text:
            return text
        for provider in sorted(self._vendors):
            for key in (self._access_key(provider), self._refresh_key(provider)):
                value = self._vault.get(key)
                if value and len(value) >= 6 and value in text:
                    text = text.replace(value, placeholder(provider))
        return text


__all__ = [
    "ConnectionStatus",
    "OAuthBroker",
    "OAuthBrokerError",
    "OAuthVendor",
    "TokenSet",
    "placeholder",
    "provider_of",
]

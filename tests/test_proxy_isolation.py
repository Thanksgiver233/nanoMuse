"""The test environment is isolated from the shell's proxy variables.

A ``NO_PROXY`` carrying bracketed IPv6 literals once broke httpx client
construction across the suite (``Invalid port: ':1]'``) on machines behind a
proxy. The autouse fixture in ``conftest.py`` clears the proxy variables before
every test; these tests pin that contract, and the ``dead_proxy`` fixture stays
available for the tests that need a proxy present.
"""

from __future__ import annotations

import os

import httpx

_PROXY_VARIABLES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def test_no_proxy_variable_reaches_a_test() -> None:
    assert not any(name in os.environ for name in _PROXY_VARIABLES)


def test_an_httpx_client_builds_without_the_shells_proxy() -> None:
    client = httpx.Client(trust_env=True)
    try:
        assert client._transport is not None
    finally:
        client.close()


def test_the_dead_proxy_fixture_still_sets_a_proxy(dead_proxy: str) -> None:
    assert os.environ["HTTP_PROXY"] == dead_proxy
    assert "NO_PROXY" not in os.environ

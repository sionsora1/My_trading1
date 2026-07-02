"""Unit tests for API authentication guards."""

import pytest
from fastapi import HTTPException


class DummyRequest:
    """Minimal request object for auth guard tests."""

    def __init__(self, headers=None, query_params=None):
        """Create a request with headers and query params.

        Args:
            headers: Mapping of HTTP headers.
            query_params: Mapping of query-string params.
        """
        self.headers = headers or {}
        self.query_params = query_params or {}


def test_live_write_rejects_referer_without_token(monkeypatch):
    """Live write auth must not trust same-origin Referer."""
    import server

    monkeypatch.setattr(server, 'API_KEY', 'unit-test-secret')

    with pytest.raises(HTTPException) as exc:
        server.require_live_write_auth(
            DummyRequest(
                headers={
                    'Referer': 'http://localhost:8000/live.html',
                    'Host': 'localhost:8000',
                }
            )
        )

    assert exc.value.status_code == 401


def test_live_write_rejects_insecure_default_api_key(monkeypatch):
    """Live write auth requires an explicitly configured QUANT_API_KEY."""
    import server

    monkeypatch.setattr(server, 'API_KEY', 'quant-trading-2026')

    with pytest.raises(HTTPException) as exc:
        server.require_live_write_auth(
            DummyRequest(headers={'Authorization': 'Bearer quant-trading-2026'})
        )

    assert exc.value.status_code == 503


def test_live_write_accepts_explicit_bearer_token(monkeypatch):
    """Valid bearer tokens pass the strong live-write auth guard."""
    import server

    monkeypatch.setattr(server, 'API_KEY', 'unit-test-secret')

    assert (
        server.require_live_write_auth(
            DummyRequest(headers={'Authorization': 'Bearer unit-test-secret'})
        )
        is None
    )


def test_live_write_accepts_explicit_query_token(monkeypatch):
    """Valid query tokens are supported for CLI compatibility."""
    import server

    monkeypatch.setattr(server, 'API_KEY', 'unit-test-secret')

    assert (
        server.require_live_write_auth(
            DummyRequest(query_params={'api_key': 'unit-test-secret'})
        )
        is None
    )


def test_sensitive_write_routes_use_strong_auth_dependency():
    """Sensitive write routes are registered with the strong auth guard."""
    import server

    expected_paths = {
        '/api/live/order',
        '/api/live/order/{order_id}/cancel',
        '/api/live/signal/confirm',
        '/api/live/scan',
        '/api/live/start',
        '/api/live/config',
        '/api/live/stop',
        '/api/live/reset',
        '/api/live/pool/add',
        '/api/live/pool/{code}',
        '/api/live/pool/import',
        '/api/live/pool/import-text',
        '/api/live/trade/record',
        '/api/live/trade/checklist/{item_id}/done',
        '/api/live/trade/checklist/{item_id}/skip',
        '/api/pool/sync-to-live',
        '/api/monitor/start',
        '/api/monitor/stop',
        '/api/watcher/start',
        '/api/watcher/stop',
    }

    protected_paths = set()
    for route in server.app.routes:
        dependencies = getattr(route, 'dependencies', [])
        if any(dep.dependency is server.require_live_write_auth for dep in dependencies):
            protected_paths.add(route.path)

    assert expected_paths <= protected_paths

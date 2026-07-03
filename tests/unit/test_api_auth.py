"""Unit tests for API authentication guards."""

from pathlib import Path

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


def test_sensitive_stream_routes_use_strong_auth_dependency():
    """Live SSE streams expose real-time data and must require API auth."""
    import server

    expected_paths = {
        '/api/monitor/stream',
        '/api/watcher/stream',
    }

    protected_paths = set()
    for route in server.app.routes:
        dependencies = getattr(route, 'dependencies', [])
        if any(dep.dependency is server.require_live_write_auth for dep in dependencies):
            protected_paths.add(route.path)

    assert expected_paths <= protected_paths


def test_sensitive_monitor_read_routes_use_strong_auth_dependency():
    """Monitor and watcher reads expose real-time surveillance state."""
    import server

    expected_paths = {
        '/api/monitor/status',
        '/api/monitor/history',
        '/api/watcher/status',
    }

    protected_paths = set()
    for route in server.app.routes:
        dependencies = getattr(route, 'dependencies', [])
        if any(dep.dependency is server.require_live_write_auth for dep in dependencies):
            protected_paths.add(route.path)

    assert expected_paths <= protected_paths


def test_sensitive_read_routes_use_strong_auth_dependency():
    """Sensitive read routes such as logs must require API auth."""
    import server

    expected_paths = {
        '/api/logs',
        '/api/logs/download',
    }

    protected_paths = set()
    for route in server.app.routes:
        dependencies = getattr(route, 'dependencies', [])
        if any(dep.dependency is server.require_live_write_auth for dep in dependencies):
            protected_paths.add(route.path)

    assert expected_paths <= protected_paths


def test_sensitive_live_read_routes_use_strong_auth_dependency():
    """Live account, orders, signals, and checklist reads expose trading data."""
    import server

    expected_paths = {
        '/api/live/status',
        '/api/live/account',
        '/api/live/positions',
        '/api/live/orders',
        '/api/live/signals',
        '/api/live/signals/history',
        '/api/live/trade/checklist',
        '/api/live/pool',
        '/api/live/pool/export',
    }

    protected_paths = set()
    for route in server.app.routes:
        dependencies = getattr(route, 'dependencies', [])
        if any(dep.dependency is server.require_live_write_auth for dep in dependencies):
            protected_paths.add(route.path)

    assert expected_paths <= protected_paths


def test_sensitive_pool_routes_use_strong_auth_dependency():
    """Stock-pool reads and exports expose configured trading universes."""
    import server

    expected_paths = {
        '/api/pool/export',
        '/api/pool/import',
        '/api/pool/sync-from-live',
        '/api/pool/sync-to-live',
    }

    protected_paths = set()
    for route in server.app.routes:
        dependencies = getattr(route, 'dependencies', [])
        if any(dep.dependency is server.require_live_write_auth for dep in dependencies):
            protected_paths.add(route.path)

    assert expected_paths <= protected_paths


def test_sensitive_backtest_routes_use_strong_auth_dependency():
    """Backtest tasks and results expose strategy inputs and performance."""
    import server

    expected_paths = {
        '/api/backtest',
        '/api/tasks',
        '/api/tasks/{task_id}',
        '/api/results/{task_id}',
        '/api/results/{task_id}/daily',
        '/api/results/{task_id}/chart',
    }

    protected_paths = set()
    for route in server.app.routes:
        dependencies = getattr(route, 'dependencies', [])
        if any(dep.dependency is server.require_live_write_auth for dep in dependencies):
            protected_paths.add(route.path)

    assert expected_paths <= protected_paths


def test_sensitive_database_routes_use_strong_auth_dependency():
    """Database explorer routes expose local market data and exports."""
    import server

    expected_paths = {
        '/api/db/stats',
        '/api/db/stocks',
        '/api/db/kline/{code}',
        '/api/db/depth/{code}',
        '/api/db/minute/{code}',
        '/api/db/financial/{code}',
        '/api/db/compare',
        '/api/db/kline/{code}/export',
    }

    protected_paths = set()
    for route in server.app.routes:
        dependencies = getattr(route, 'dependencies', [])
        if any(dep.dependency is server.require_live_write_auth for dep in dependencies):
            protected_paths.add(route.path)

    assert expected_paths <= protected_paths


def test_sensitive_kline_routes_use_strong_auth_dependency():
    """K-line analysis routes expose local market data and strategy output."""
    import server

    expected_paths = {
        '/api/kline/stocks',
        '/api/kline/strategies',
        '/api/kline/data/{code}',
        '/api/kline/minute/{code}',
        '/api/kline/minute/reversal/{code}',
        '/api/kline/regression/{code}',
        '/api/kline/strategy',
    }

    protected_paths = set()
    for route in server.app.routes:
        dependencies = getattr(route, 'dependencies', [])
        if any(dep.dependency is server.require_live_write_auth for dep in dependencies):
            protected_paths.add(route.path)

    assert expected_paths <= protected_paths


def test_live_page_sends_auth_for_protected_write_requests():
    """Live page write actions must send the API key to protected routes."""
    html = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath('web', 'live.html')
        .read_text(encoding='utf-8')
    )

    assert "const API_KEY_STORAGE = 'quantApiKey';" in html
    assert 'function writeFetch(url, options)' in html
    assert "headers: authHeaders({'Content-Type':'application/json'})," in html

    protected_direct_fetches = {
        "fetch('/api/monitor/start'",
        "fetch('/api/monitor/stop'",
        "fetch('/api/watcher/start'",
        "fetch('/api/watcher/stop'",
        "fetch(`${BASE}/pool/${code}`",
    }
    for direct_fetch in protected_direct_fetches:
        assert direct_fetch not in html


def test_live_pages_send_auth_for_sensitive_read_requests():
    """Live pages must send API auth for sensitive GET endpoints."""
    root = Path(__file__).resolve().parents[2]
    live_html = root.joinpath('web', 'live.html').read_text(encoding='utf-8')
    mobile_html = root.joinpath('web', 'mobile.html').read_text(encoding='utf-8')
    index_html = root.joinpath('web', 'index.html').read_text(encoding='utf-8')

    assert "fetch(`${BASE}${path}`, { headers: authHeaders() })" in live_html
    assert "fetch(`${BASE}${path}`, { headers: authHeaders() })" in mobile_html
    assert "fetch('/api/live/status', {headers:liveAuthHeaders()})" in index_html


def test_database_explorer_sends_auth_for_database_requests():
    """Database explorer must authenticate API_BASE requests and exports."""
    html = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath('web', 'db_explorer.html')
        .read_text(encoding='utf-8')
    )

    assert 'function dbHeaders(extra)' in html
    assert 'function dbJson(url, options)' in html
    assert 'function dbUrl(path)' in html
    assert 'fetch(`${API_BASE}/' not in html
    assert 'fetch(API_BASE' not in html
    assert 'a.href = dbUrl(' in html


def test_database_explorer_sends_auth_for_kline_requests():
    """Database explorer must authenticate KLINE_API requests."""
    html = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath('web', 'db_explorer.html')
        .read_text(encoding='utf-8')
    )

    assert 'function dbFetch(url, options)' in html
    assert 'function dbJson(url, options)' in html
    assert 'fetch(`${KLINE_API}/' not in html
    assert 'fetch(KLINE_API' not in html


def test_kline_page_sends_auth_for_kline_requests():
    """K-line visualization page must authenticate all /api/kline calls."""
    html = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath('web', 'kline_vis.html')
        .read_text(encoding='utf-8')
    )

    assert "const API_KEY_STORAGE = 'quantApiKey';" in html
    assert 'function klineFetch(url, options)' in html
    assert 'function klineJson(url, options)' in html
    assert "fetch('/api/kline" not in html
    assert 'fetch(`/api/kline' not in html


def test_app_page_sends_auth_for_backtest_and_pool_requests():
    """Backtest page must authenticate protected pool and result requests."""
    html = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath('web', 'app.html')
        .read_text(encoding='utf-8')
    )

    assert "const API_KEY_STORAGE = 'quantApiKey';" in html
    assert 'function authFetch(url, options)' in html
    assert 'function authJson(url, options)' in html
    assert "authJson(API_BASE+'/pool/sync-from-live')" in html
    assert "authFetch(API_BASE+'/pool/sync-to-live'" in html
    assert "authFetch(API_BASE + '/backtest'" in html
    assert "authFetch(API_BASE + '/tasks/' + btCurrentTaskId)" in html
    assert "authFetch(API_BASE + '/results/' + btCurrentTaskId)" in html
    assert "authJson(API_BASE + '/results/' + btCurrentTaskId)" in html
    assert "authJson(API_BASE + '/results/' + btCurrentTaskId + '/daily?strategy='" in html
    assert "fetch(API_BASE + '/backtest'" not in html
    assert "fetch(API_BASE + '/tasks/' + btCurrentTaskId)" not in html
    assert "fetch(API_BASE + '/results/' + btCurrentTaskId)" not in html
    assert "fetch(API_BASE+'/pool/sync-from-live')" not in html


def test_live_page_sends_auth_for_sensitive_event_streams():
    """Live page SSE connections must include the API key query token."""
    html = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath('web', 'live.html')
        .read_text(encoding='utf-8')
    )

    assert 'function authedStreamUrl(path)' in html
    assert "new EventSource(authedStreamUrl('/api/monitor/stream'))" in html
    assert "new EventSource(authedStreamUrl('/api/watcher/stream'))" in html
    assert "new EventSource('/api/monitor/stream')" not in html
    assert "new EventSource('/api/watcher/stream')" not in html


def test_live_page_sends_auth_for_monitor_read_requests():
    """Live page monitor and watcher reads must send API auth."""
    html = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath('web', 'live.html')
        .read_text(encoding='utf-8')
    )

    assert "authJson('/api/monitor/status')" in html
    assert "authJson('/api/monitor/history?limit=100')" in html
    assert "authJson('/api/watcher/status')" in html
    assert "fetch('/api/monitor/status')" not in html
    assert "fetch('/api/monitor/history?limit=100')" not in html
    assert "fetch('/api/watcher/status')" not in html


def test_live_page_does_not_mark_stop_success_on_auth_failure():
    """Stop buttons should not flip UI to stopped when protected calls fail."""
    html = (
        Path(__file__)
        .resolve()
        .parents[2]
        .joinpath('web', 'live.html')
        .read_text(encoding='utf-8')
    )

    assert "if (r.status !== 'stopped')" in html
    assert "showToast('停止失败: ' + (r.message || r.detail || '未知错误'), 'error')" in html
    assert "if (r.status && r.status !== 'stopped')" in html
    assert "showToast('停止盯盘失败: ' + (r.message || r.detail || '未知错误'), 'error')" in html

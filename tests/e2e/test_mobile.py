"""
tests/e2e/test_mobile.py — Playwright E2E tests for the mobile page.

Covers:
  - TC: Mobile page loads with mobile viewport (375x812)
  - TC: Mobile touch interaction (tabs, buttons)

Uses ``server_url`` and ``browser`` fixtures from tests/e2e/conftest.py.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mobile_page(browser, server_url):
    """A fresh page with iPhone-sized viewport (375x812).

    Each test gets its own isolated context, so state does not leak.
    Uses the session-scoped ``browser`` from conftest.py.
    """
    ctx = browser.new_context(
        viewport={"width": 375, "height": 812},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
    )
    pg = ctx.new_page()
    try:
        pg.goto(f"{server_url}/mobile.html", timeout=10_000)
    except Exception:
        ctx.close()
        pytest.skip(f"Server not reachable at {server_url}")

    yield pg
    ctx.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.e2e
class TestMobilePage:
    """E2E tests for the quant_strategy mobile trading-signal page."""

    def test_mobile_page_loads(self, mobile_page):
        """TC: Mobile page loads successfully with mobile viewport."""
        assert mobile_page.title(), "Page should have a non-empty title"

        # Check for key mobile UI elements
        header = mobile_page.query_selector("h1, .header, header")
        if header:
            text = (header.text_content() or "").strip()
            assert len(text) > 0, "Header should contain text"

        # Account bar
        acct = mobile_page.query_selector(
            ".account-bar, #accountBar, .stats"
        )
        assert acct is not None, "Account/stats bar should be present"

        # Tabs
        tabs = mobile_page.query_selector_all(".tab, [role='tab']")
        assert len(tabs) >= 1, "At least one tab should be present"

        # Signals container
        signals = mobile_page.query_selector(
            "#signalsContainer, #signalsEmpty, .signals-list"
        )
        assert signals is not None, "Signals container/empty-state should be present"

    def test_mobile_touch_interaction(self, mobile_page):
        """TC: Mobile touch interaction — tap tabs, buttons."""
        # Tap on checklist tab if present
        checklist_tab = mobile_page.query_selector(
            ".tab:has-text('清单'), [role='tab']:has-text('清单')"
        )
        if checklist_tab:
            checklist_tab.tap()
            mobile_page.wait_for_timeout(500)
            container = mobile_page.query_selector("#checklistContainer")
            assert container is not None, "Checklist container should exist after tapping tab"

        # Tap back to signals tab
        signals_tab = mobile_page.query_selector(
            ".tab:has-text('信号'), [role='tab']:has-text('信号')"
        )
        if signals_tab:
            signals_tab.tap()
            mobile_page.wait_for_timeout(500)

        # Tap refresh button
        refresh_btn = mobile_page.query_selector(
            "button:has-text('刷新'), #btnRefresh, .refresh-btn"
        )
        if refresh_btn:
            refresh_btn.tap()
            mobile_page.wait_for_timeout(1000)

        # Verify scrollable content
        body_height = mobile_page.evaluate("document.body.scrollHeight")
        assert body_height > 0, "Body should have content"

    def test_mobile_empty_state(self, mobile_page):
        """TC: Mobile page shows empty state or signals, not a blank page."""
        # Either the signals container or empty state should have content
        signals = mobile_page.query_selector("#signalsContainer")
        empty = mobile_page.query_selector("#signalsEmpty")

        has_signals = signals is not None and len(
            (signals.text_content() or "").strip()
        ) > 0
        has_empty = empty is not None and empty.is_visible()

        assert has_signals or has_empty, (
            "Mobile page should show either signals or empty state, not blank"
        )

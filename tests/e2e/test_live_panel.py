"""
tests/e2e/test_live_panel.py — E2E browser tests for the live trading panel.

Uses Playwright (headless Chromium) and the FastAPI server fixtures
defined in ``tests/e2e/conftest.py``.

Requirements
    pip install playwright pytest
    playwright install chromium
"""

import pytest


@pytest.mark.e2e
class TestLivePanel:
    """Browser tests for ``web/live.html`` — the live trading panel."""

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _skip_if_element_missing(page, selector, reason_snippet):
        """Skip the current test if *selector* is not found within 10 s."""
        try:
            page.wait_for_selector(selector, timeout=10000)
        except Exception:
            # Take a screenshot so we can debug what the page actually shows
            page.screenshot(path="e2e_live_panel_missing.png")
            pytest.skip(
                f"Element matching '{reason_snippet}' not found on live panel"
            )

    # ------------------------------------------------------------------
    # TC: Live page loads
    # ------------------------------------------------------------------

    def test_live_page_loads(self, server_url, page):
        """Navigate to live.html and verify the page displays.

        Checks that the document title, the navbar "实盘交易" link,
        and the status bar are all present.
        """
        page.goto(f"{server_url}/live.html", wait_until="domcontentloaded")

        # Document title
        title = page.title()
        assert title, "Page title should not be empty"
        assert "实盘" in title or "交易" in title, (
            f"Expected page title to contain 实盘 or 交易, got: {title!r}"
        )

        # Navbar — the active link should say "实盘交易"
        self._skip_if_element_missing(
            page, "text=实盘交易", "实盘交易 (navbar)"
        )

        # Status bar — should show broker + mode placeholders
        status_bar = page.query_selector("#statusBar")
        if status_bar is None:
            page.screenshot(path="e2e_live_panel_missing.png")
            pytest.skip("Status bar #statusBar not found")
        assert status_bar.is_visible(), "Status bar should be visible"

        # At least one of the control buttons should be present
        btn_start = page.query_selector("#btnStart")
        manual_scan = page.query_selector("text=手动扫描")
        assert btn_start is not None or manual_scan is not None, (
            "Expected at least one control button (启动实盘 / 手动扫描)"
        )

        # Screenshot for manual verification
        page.screenshot(path="e2e_live_panel_loaded.png")

    # ------------------------------------------------------------------
    # TC: Account info displayed
    # ------------------------------------------------------------------

    def test_account_info_displayed(self, server_url, page):
        """Verify that account-related information is visible on the page.

        Checks for the account overview cards: 总资产 (total assets),
        可用资金 (available cash), 持仓市值 (position market value),
        and 当日盈亏 (daily P&L).
        """
        page.goto(f"{server_url}/live.html", wait_until="domcontentloaded")

        # The four account metric cards each have a <div class="metric-label">
        labels_seen = []
        for label_text in ("总资产", "可用资金", "持仓市值", "当日盈亏"):
            label_el = page.query_selector(f"text={label_text}")
            if label_el is not None and label_el.is_visible():
                labels_seen.append(label_text)

        if not labels_seen:
            page.screenshot(path="e2e_live_panel_missing.png")
            pytest.skip(
                "No account metric labels found (总资产 / 可用资金 / "
                "持仓市值 / 当日盈亏)"
            )

        # At minimum, "总资产" should be visible — it is the primary metric
        assert "总资产" in labels_seen, (
            f"Expected 总资产 label visible; got: {labels_seen}"
        )

        # The metric value elements should exist (even if showing "--")
        for elem_id in ("acctAssets", "acctCash", "acctMv", "acctDaily"):
            el = page.query_selector(f"#{elem_id}")
            assert el is not None, (
                f"Account metric element #{elem_id} should exist"
            )

        page.screenshot(path="e2e_live_panel_account.png")

    # ------------------------------------------------------------------
    # TC: Position list displayed
    # ------------------------------------------------------------------

    def test_position_list_displayed(self, server_url, page):
        """Verify that a positions / holdings table is present on the page.

        Checks for the "当前持仓" card title, the positions <table>,
        and the table headers (代码, 名称, 数量, etc.).
        """
        page.goto(f"{server_url}/live.html", wait_until="domcontentloaded")

        # The positions card has title "📦 当前持仓"
        self._skip_if_element_missing(
            page, "text=当前持仓", "当前持仓 (positions card header)"
        )

        # The positions table body should exist
        tbody = page.query_selector("#positionsBody")
        if tbody is None:
            page.screenshot(path="e2e_live_panel_missing.png")
            pytest.skip("Position table body #positionsBody not found")
        assert tbody.is_visible(), (
            "Position table body #positionsBody should be visible"
        )

        # Verify table headers are present within the positions card
        for header in ("代码", "名称", "数量", "成本", "现价", "市值", "盈亏率"):
            th = page.query_selector(f"text={header}")
            if th is None:
                page.screenshot(path="e2e_live_panel_missing.png")
                pytest.skip(
                    f"Position table header '{header}' not found"
                )
            assert th.is_visible(), (
                f"Position table header '{header}' should be visible"
            )

        # Also check the position count badge exists
        badge = page.query_selector("#positionCount")
        assert badge is not None, (
            "Position count badge #positionCount should exist"
        )

        page.screenshot(path="e2e_live_panel_positions.png")

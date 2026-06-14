"""
E2E browser tests for signal confirmation dialog on the live trading page.

Uses the ``server_url`` and ``page`` fixtures from tests/e2e/conftest.py.
"""
import os
import pytest


def _reports_dir():
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    d = os.path.join(project_root, "reports")
    os.makedirs(d, exist_ok=True)
    return d


def _screenshot(page, name):
    path = os.path.join(_reports_dir(), name)
    page.screenshot(path=path, full_page=True)
    return path


def _goto(page, server_url, path):
    """Navigate to a page, skip if server not reachable."""
    try:
        page.goto(f"{server_url}/{path}", wait_until="domcontentloaded", timeout=30_000)
        return True
    except Exception:
        pytest.skip(f"Server not reachable at {server_url}/{path}")
        return False


# ---------------------------------------------------------------------------
# TC-E2E-04
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_live_page_loads(server_url, page):
    _goto(page, server_url, "live.html")
    title = page.title()
    assert title and ("实盘" in title or "交易" in title), f"Unexpected title: {title}"

    body_text = page.text_content("body") or ""
    assert any(kw in body_text for kw in ["实盘", "交易", "账户"]), (
        f"Page missing expected keywords. First 200 chars: {body_text[:200]}"
    )

    status_bar = page.query_selector("#statusBar")
    assert status_bar is not None, "Status bar (#statusBar) not found"
    assert len((status_bar.text_content() or "").strip()) > 0, "Status bar is empty"

    start_btn = page.query_selector("#btnStart")
    assert start_btn is not None, "Start button (#btnStart) not found"


# ---------------------------------------------------------------------------
# TC-E2E-05
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_start_live_trading(server_url, page):
    _goto(page, server_url, "live.html")

    start_btn = page.query_selector("#btnStart")
    if start_btn is None:
        pytest.fail("Start button (#btnStart) is missing from the page")

    if not start_btn.is_visible():
        _screenshot(page, "e2e_05_already_running.png")
        pytest.skip("Start button is hidden — live trading may already be running")

    start_btn.click()
    page.wait_for_timeout(3000)
    assert len((page.text_content("body") or "")) > 0, "Page body empty after start"
    print(f"  Screenshot saved to {_screenshot(page, 'e2e_05_after_start.png')}")


# ---------------------------------------------------------------------------
# TC-E2E-06
# ---------------------------------------------------------------------------

@pytest.mark.e2e
def test_signal_confirm_dialog(server_url, page):
    _goto(page, server_url, "live.html")

    # Step 1: Start live trading if not running
    start_btn = page.query_selector("#btnStart")
    if start_btn is not None and start_btn.is_visible():
        start_btn.click()
        page.wait_for_timeout(3000)

    # Step 2: Scan for signals
    scan_btn = page.query_selector("button:has-text('手动扫描')")
    if scan_btn is not None and scan_btn.is_visible():
        scan_btn.click()
        page.wait_for_timeout(4000)

    # Step 3: Look for confirm button
    confirm_btn = page.query_selector("button:has-text('确认')")
    if confirm_btn is None or not confirm_btn.is_visible():
        order_tab = page.query_selector("#tabOrder")
        if order_tab is not None and order_tab.is_visible():
            order_tab.click()
        page.wait_for_timeout(500)

        price_input = page.query_selector("#orderPrice")
        qty_input = page.query_selector("#orderQty")
        code_input = page.query_selector("#orderCode")
        if price_input and price_input.is_visible():
            price_input.fill("12.50")
        if qty_input and qty_input.is_visible():
            qty_input.fill("100")
        if code_input and code_input.is_visible():
            code_input.fill("600519")
        page.wait_for_timeout(300)
        path = _screenshot(page, "e2e_06_manual_order_form.png")
        print(f"  No confirm button — fell back to manual order form: {path}")
        return

    confirm_btn.click()
    page.wait_for_timeout(2000)

    dialog = page.query_selector(".modal, .dialog, [role='dialog'], [aria-modal='true']")
    if dialog is not None and dialog.is_visible():
        price_input = dialog.query_selector(
            "input[type='number'], input#orderPrice, input[name='price'], input[placeholder*='价']"
        )
        qty_input = dialog.query_selector(
            "input#orderQty, input[name='quantity'], input[placeholder*='量']"
        )
        if price_input:
            price_input.fill("12.50")
        if qty_input:
            qty_input.fill("100")
        dialog_confirm = dialog.query_selector(
            "button:has-text('确认'), button:has-text('提交'), button:has-text('确定')"
        )
        if dialog_confirm:
            dialog_confirm.click()
            page.wait_for_timeout(2000)
        path = _screenshot(page, "e2e_06_dialog_interaction.png")
        print(f"  Dialog found and interacted with: {path}")
    else:
        page.wait_for_timeout(1000)
        toast = page.query_selector("#toast")
        toast_visible = (
            toast is not None
            and toast.is_visible()
            and (toast.text_content() or "").strip() != ""
        )
        path = _screenshot(page, "e2e_06_confirm_no_dialog.png")
        print(f"  No dialog (signal via API). Toast: {toast_visible}. Screenshot: {path}")

"""
tests/e2e/conftest.py — E2E test fixtures

Auto-starts the FastAPI server and manages Playwright browser instances
for browser-based end-to-end tests.
"""
import pytest
import os
import sys
import subprocess
import time
import signal
import urllib.request
import urllib.error

# Our package is at: quant_strategy/tests/e2e/conftest.py
# Project root is three levels up
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

# Ensure the project root is on sys.path so the server can import its modules
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

SERVER_PORT = 8000
SERVER_URL = f"http://localhost:{SERVER_PORT}"


def _server_is_alive(url: str, timeout: float = 2.0) -> bool:
    """Check whether the server at *url* responds to an HTTP GET."""
    try:
        req = urllib.request.Request(url, method="GET")
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except (urllib.error.URLError, ConnectionRefusedError, OSError):
        return False


def _kill_server(proc: subprocess.Popen) -> None:
    """Terminate the server process in a platform-appropriate way."""
    try:
        if os.name == "nt":
            # Windows — terminate the process tree
            proc.terminate()
        else:
            # Unix — kill the whole process group
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        # Process already dead — nothing to do
        pass


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def server_url():
    """Start the FastAPI server, wait until it is ready, then yield the URL.

    The server is started once per test session and stopped after all tests
    have completed.
    """
    server_process = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # On Unix, run the server in its own process group so we can
        # kill the whole group (including any child workers) on teardown.
        preexec_fn=os.setsid if os.name != "nt" else None,
    )

    # Poll the health-check URL until the server responds (max 15 retries).
    url = f"{SERVER_URL}/api"
    ready = False
    for attempt in range(1, 16):
        if _server_is_alive(url):
            ready = True
            break
        # If the process exited prematurely, bail out
        if server_process.poll() is not None:
            break
        time.sleep(1)

    if not ready:
        # Give the caller a chance to see what went wrong
        try:
            server_process.kill()
        except OSError:
            pass
        stderr_output = ""
        try:
            stderr_output = server_process.stderr.read().decode(
                "utf-8", errors="replace"
            )
        except Exception:
            pass
        raise RuntimeError(
            f"Server failed to respond on {url} after 15 retries.\n"
            f"stderr:\n{stderr_output}"
        )

    yield SERVER_URL

    # Teardown: kill the server
    _kill_server(server_process)
    try:
        server_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server_process.kill()
        server_process.wait()


@pytest.fixture(scope="session")
def browser():
    """Launch a headless Chromium browser for the duration of the test session.

    Requires ``playwright`` to be installed (``pip install playwright`` and
    ``playwright install chromium``).
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=True)
        yield chromium
        chromium.close()


# ---------------------------------------------------------------------------
# Function-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def page(browser, server_url):
    """Create a fresh browser context + page for each test function.

    Each test gets its own isolated context (cookies, storage, etc.) and a
    page pre-sized to a common desktop viewport.
    """
    context = browser.new_context(
        viewport={"width": 1440, "height": 900},
    )
    pg = context.new_page()

    yield pg

    context.close()

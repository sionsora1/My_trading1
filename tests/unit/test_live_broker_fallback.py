"""Unit tests for live broker fallback visibility."""


class FakeAccount:
    """Minimal account object for status serialization."""

    total_assets = 100000
    available_cash = 100000
    market_value = 0
    total_profit = 0
    total_profit_rate = 0
    daily_profit = 0
    position_count = 0
    update_time = '2026-07-02 09:30:00'


class FakeBroker:
    """Small fake broker with configurable connection result."""

    def __init__(self, connected=True):
        """Create fake broker.

        Args:
            connected: Whether connect() should succeed.
        """
        self.connected = connected

    def connect(self):
        """Return the configured connection result."""
        return self.connected

    def get_account(self):
        """Return a minimal account."""
        return FakeAccount()

    def get_positions_list(self):
        """Return no positions."""
        return []


class FakeRiskManager:
    """Small fake risk manager."""

    def get_status(self):
        """Return minimal risk status."""
        return {}


class FakeNotifier:
    """Small fake notifier."""

    def get_signal_stats(self):
        """Return minimal signal stats."""
        return {}


def test_qmt_fallback_status_preserves_requested_and_active_broker(monkeypatch):
    """QMT connection failure is visible as requested qmt and active sim."""
    import live_server

    def fake_get_broker(name, config=None):
        return FakeBroker(connected=name == 'sim')

    monkeypatch.setattr(live_server, 'get_broker', fake_get_broker)
    monkeypatch.setattr(
        live_server.LiveTradingServer,
        '_init_risk_manager',
        lambda self: setattr(self, 'risk_manager', FakeRiskManager()),
    )
    monkeypatch.setattr(
        live_server.LiveTradingServer,
        '_init_notifier',
        lambda self: setattr(self, 'notifier', FakeNotifier()),
    )
    monkeypatch.setattr(live_server.LiveTradingServer, '_init_data', lambda self: None)
    monkeypatch.setattr(live_server.LiveTradingServer, '_init_signal_bus', lambda self: None)

    server = live_server.LiveTradingServer(
        {
            'broker': 'qmt',
            'mode': 'semi',
            'qmt': {},
            'sim': {},
        }
    )

    status = server.get_status()

    assert status['requested_broker'] == 'qmt'
    assert status['active_broker'] == 'sim'
    assert status['broker_fallback'] is True
    assert status['broker_connection_failed'] is True
    assert status['broker_fallback_reason']
    assert status['broker_status'] == {
        'requested_broker': 'qmt',
        'active_broker': 'sim',
        'fallback': True,
        'connection_failed': True,
        'fallback_reason': status['broker_fallback_reason'],
    }

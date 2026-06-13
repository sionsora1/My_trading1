"""
Strategy Profiles — presets for different market regimes.

Each profile defines a set of strategies, a position ratio, and a stop-loss
level suitable for a particular market environment.

Usage:
    from config.strategy_profiles import get_profile_for_regime, get_active_profile
    from analysis.market_regime import MarketRegime

    profile = get_profile_for_regime(MarketRegime.BULL)
    active  = get_active_profile()   # respects MANUAL_LOCK
"""

from analysis.market_regime import MarketRegime

# ---------------------------------------------------------------------------
# Strategy profiles indexed by regime name (lowercase string)
# ---------------------------------------------------------------------------
STRATEGY_PROFILES = {
    'bull': {
        'name': '牛市组合',
        'strategies': ['trend_following', 'momentum', 'eight_factor'],
        'position_ratio': 0.80,
        'stop_loss': -0.08,
    },
    'bear': {
        'name': '熊市组合',
        'strategies': ['low_volatility', 'value', 'quality'],
        'position_ratio': 0.30,
        'stop_loss': -0.05,
    },
    'sideways': {
        'name': '震荡市组合',
        'strategies': ['mean_reversion', 'intraday_reversal', 'eight_factor'],
        'position_ratio': 0.50,
        'stop_loss': -0.06,
    },
    'volatile': {
        'name': '高波动组合',
        'strategies': ['low_volatility', 'intraday_reversal'],
        'position_ratio': 0.30,
        'stop_loss': -0.04,
    },
    'crash': {
        'name': '暴跌组合',
        'strategies': ['low_volatility'],
        'position_ratio': 0.10,
        'stop_loss': -0.03,
    },
    'default': {
        'name': '默认组合',
        'strategies': ['eight_factor', 'trend_following', 'mean_reversion'],
        'position_ratio': 0.60,
        'stop_loss': -0.08,
    },
}

# ---------------------------------------------------------------------------
# Manual lock — when enabled the system ignores regime detection and uses
# the locked profile exclusively.
# ---------------------------------------------------------------------------
MANUAL_LOCK = {
    'enabled': False,
    'profile': None,  # e.g. 'bull' or 'bear'
}

# ---------------------------------------------------------------------------
# Signal-bus / execution configuration
# ---------------------------------------------------------------------------
SIGNAL_BUS_CONFIG = {
    'max_positions': 5,
    'min_order_amount': 2000,
    'signal_timeout_minutes': 30,
    'max_daily_trades': 10,
}

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

# Map MarketRegime enum values to profile keys
_REGIME_TO_PROFILE = {
    MarketRegime.BULL: 'bull',
    MarketRegime.BEAR: 'bear',
    MarketRegime.SIDEWAYS: 'sideways',
    MarketRegime.VOLATILE: 'volatile',
    MarketRegime.CRASH: 'crash',
}


def get_profile_for_regime(regime_name) -> dict:
    """Return the strategy profile for a given market regime.

    Args:
        regime_name:
            - A MarketRegime enum value, e.g. ``MarketRegime.BULL``
            - Or a string key like ``'bull'``, ``'bear'``, etc.

    Returns:
        The matching profile dict.  Falls back to ``'default'`` when the
        regime is unknown.
    """
    # Accept both MarketRegime enum and plain string
    if isinstance(regime_name, MarketRegime):
        key = _REGIME_TO_PROFILE.get(regime_name, 'default')
    else:
        key = str(regime_name).lower()
        if key not in STRATEGY_PROFILES:
            key = 'default'

    return STRATEGY_PROFILES.get(key, STRATEGY_PROFILES['default'])


def get_active_profile() -> dict:
    """Return the currently active strategy profile.

    If ``MANUAL_LOCK['enabled']`` is True and a profile is set, that profile
    is returned regardless of market conditions.  Otherwise the default
    profile is used.

    Returns:
        Profile dict (same shape as values in ``STRATEGY_PROFILES``).
    """
    if MANUAL_LOCK.get('enabled') and MANUAL_LOCK.get('profile'):
        profile_key = MANUAL_LOCK['profile']
        if profile_key in STRATEGY_PROFILES:
            return STRATEGY_PROFILES[profile_key]

    return STRATEGY_PROFILES['default']

"""
Web API routes for the quant trading system.
Provides endpoints for signal confirmation, strategy management, and data status.

Include in your FastAPI app with:
    from web.api import router as web_api_router
    app.include_router(web_api_router)
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import APIRouter, HTTPException, Body
from fastapi.responses import JSONResponse
from typing import Optional
from datetime import datetime

from strategy import get_all_strategies, STRATEGY_REGISTRY
from config.strategy_profiles import (
    STRATEGY_PROFILES, MANUAL_LOCK, SIGNAL_BUS_CONFIG, get_active_profile
)

router = APIRouter()


# ============================================================
# Helpers – lazy access to live server and its internals
# ============================================================

def _get_live_server():
    """Lazy-load the live server singleton (avoids import cycles)."""
    try:
        # Import here so the module loads even when the server isn't running
        from server import get_live_server as gls
        return gls()
    except Exception:
        return None


def _get_signal_bus():
    """Return the SignalBus instance if the live server has one."""
    ls = _get_live_server()
    if ls is None:
        return None
    # The live server may hold a signal_bus attribute
    if hasattr(ls, 'signal_bus') and ls.signal_bus is not None:
        return ls.signal_bus
    return None


def _get_manual_broker():
    """Return the ManualBroker instance if the live server uses one."""
    ls = _get_live_server()
    if ls is None or not hasattr(ls, 'broker'):
        return None
    broker = ls.broker
    # Check if it's a ManualBroker (has confirm_order / reject_order)
    if hasattr(broker, 'confirm_order') and hasattr(broker, 'reject_order'):
        return broker
    return None


# ============================================================
# Signal Confirmation (ManualBroker)
# ============================================================

@router.get("/api/signals/pending")
async def get_pending_signals():
    """Get pending signals awaiting user confirmation"""
    broker = _get_manual_broker()
    if broker is None:
        return JSONResponse(content={
            "status": "success",
            "data": [],
            "message": "ManualBroker not initialised — start live trading first",
        })

    try:
        pending_orders = broker.get_pending_signals()
        result = []
        for order in pending_orders:
            result.append({
                "order_id": getattr(order, 'order_id', ''),
                "ts_code": getattr(order, 'ts_code', ''),
                "side": getattr(order, 'side', None).value if hasattr(getattr(order, 'side', None), 'value') else str(getattr(order, 'side', '')),
                "price": getattr(order, 'price', 0),
                "quantity": getattr(order, 'quantity', 0),
                "status": getattr(order, 'status', None).value if hasattr(getattr(order, 'status', None), 'value') else str(getattr(order, 'status', '')),
                "create_time": getattr(order, 'create_time', ''),
                "reason": getattr(order, 'reason', ''),
            })
        return JSONResponse(content={"status": "success", "data": result})
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})


@router.post("/api/signals/{signal_id}/confirm")
async def confirm_signal(signal_id: str, body: dict):
    """User confirms signal after executing in Eastmoney app"""
    fill_price = body.get('fill_price', 0)
    fill_qty = body.get('fill_qty', 0)

    broker = _get_manual_broker()
    if broker is None:
        raise HTTPException(status_code=503, detail="ManualBroker not available")

    try:
        result = broker.confirm_order(signal_id, float(fill_price), int(fill_qty))
        if result is None:
            raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found or not in PENDING status")

        return {
            "status": "success",
            "data": {
                "order_id": getattr(result, 'order_id', signal_id),
                "filled_price": getattr(result, 'filled_price', fill_price),
                "filled_quantity": getattr(result, 'filled_quantity', fill_qty),
                "status": getattr(result, 'status', None).value if hasattr(getattr(result, 'status', None), 'value') else 'FILLED',
                "message": f"Signal {signal_id} confirmed at {fill_price} x {fill_qty}",
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/signals/{signal_id}/reject")
async def reject_signal(signal_id: str, body: dict = None):
    """User rejects a signal"""
    reason = (body or {}).get('reason', '用户手动拒绝')

    broker = _get_manual_broker()
    if broker is None:
        raise HTTPException(status_code=503, detail="ManualBroker not available")

    try:
        result = broker.reject_order(signal_id, reason)
        if result is None:
            raise HTTPException(status_code=404, detail=f"Signal {signal_id} not found or not in PENDING status")

        return {
            "status": "success",
            "data": {
                "order_id": signal_id,
                "status": "REJECTED",
                "message": f"Signal {signal_id} rejected: {reason}",
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Also expose the signal bus pending (broker-agnostic signals)
@router.get("/api/signals/bus-pending")
async def get_bus_pending_signals():
    """Get pending signals from SignalBus (strategy-side, broker-agnostic)"""
    bus = _get_signal_bus()
    if bus is None:
        return JSONResponse(content={
            "status": "success",
            "data": [],
            "message": "SignalBus not initialised",
        })
    try:
        pending = bus.get_pending_signals()
        return JSONResponse(content={"status": "success", "data": pending})
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})


@router.get("/api/signals/stats")
async def get_signal_stats():
    """Get signal statistics from SignalBus"""
    bus = _get_signal_bus()
    if bus is None:
        return JSONResponse(content={
            "status": "success",
            "data": {"total": 0, "pending": 0},
            "message": "SignalBus not initialised",
        })
    try:
        stats = bus.get_statistics()
        return JSONResponse(content={"status": "success", "data": stats})
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})


# ============================================================
# Strategy Management
# ============================================================

@router.get("/api/strategies")
async def get_strategies():
    """Get all strategies, profiles, and active profile"""
    try:
        all_strategies = get_all_strategies()
        active_profile = get_active_profile()

        # Also include the broker registry info
        try:
            from broker import get_all_brokers
            brokers = get_all_brokers()
        except Exception:
            brokers = {}

        return {
            "status": "success",
            "data": {
                "strategies": all_strategies,
                "profiles": dict(STRATEGY_PROFILES),
                "active_profile": active_profile.get('name', 'default'),
                "active_profile_detail": active_profile,
                "active_strategies": active_profile.get('strategies', list(all_strategies.keys())),
                "manual_lock": dict(MANUAL_LOCK),
                "signal_config": dict(SIGNAL_BUS_CONFIG),
                "brokers": brokers,
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/strategies/profile")
async def set_strategy_profile(body: dict):
    """Switch strategy profile or toggle manual lock"""
    try:
        profile_name = body.get('profile')
        if profile_name and profile_name in STRATEGY_PROFILES:
            MANUAL_LOCK['enabled'] = True
            MANUAL_LOCK['profile'] = profile_name
            return {
                "status": "success",
                "data": {
                    "message": f"已切换到 {STRATEGY_PROFILES[profile_name]['name']}",
                    "active_profile": STRATEGY_PROFILES[profile_name],
                    "manual_lock": dict(MANUAL_LOCK),
                }
            }

        # Custom strategy selection: explicitly set active strategies list
        if 'strategies' in body:
            custom_strategies = body['strategies']
            valid = [s for s in custom_strategies if s in STRATEGY_REGISTRY]
            if not valid:
                return {"status": "error", "message": "没有有效的策略"}
            # Store custom selection in a dedicated profile slot
            STRATEGY_PROFILES['custom'] = {
                'name': '自定义组合',
                'strategies': valid,
                'position_ratio': body.get('position_ratio', 0.60),
                'stop_loss': body.get('stop_loss', -0.08),
            }
            MANUAL_LOCK['enabled'] = True
            MANUAL_LOCK['profile'] = 'custom'
            profile = STRATEGY_PROFILES['custom']
            return {
                "status": "success",
                "data": {
                    "message": f"已启用自定义组合（{len(valid)}个策略）",
                    "active_profile": profile,
                    "manual_lock": dict(MANUAL_LOCK),
                }
            }

        # Toggle manual lock
        if 'manual_lock' in body:
            enabled = bool(body['manual_lock'])
            MANUAL_LOCK['enabled'] = enabled
            if not enabled:
                MANUAL_LOCK['profile'] = None
            elif not MANUAL_LOCK['profile']:
                MANUAL_LOCK['profile'] = 'default'

        # Reset to default (auto-detect mode)
        if body.get('reset'):
            MANUAL_LOCK['enabled'] = False
            MANUAL_LOCK['profile'] = None

        return {
            "status": "success",
            "data": {
                "message": "策略配置已更新",
                "manual_lock": dict(MANUAL_LOCK),
                "active_profile": get_active_profile(),
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# Data Status
# ============================================================

@router.get("/api/data/status")
async def data_status():
    """Get data overview: daily_bars count, minute_bars count, calendar count"""
    try:
        from data.database import SQLiteManager
        db = SQLiteManager()
        try:
            cur = db._conn
            daily_count = cur.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
            minute_count = cur.execute("SELECT COUNT(*) FROM minute_bars").fetchone()[0]
            calendar_count = cur.execute("SELECT COUNT(*) FROM trade_calendar").fetchone()[0]
            fundamentals_count = cur.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
            stock_count = cur.execute("SELECT COUNT(*) FROM stock_info").fetchone()[0]

            # Latest trade date
            latest_row = cur.execute("SELECT MAX(trade_date) FROM daily_bars").fetchone()
            latest_date = latest_row[0] if latest_row and latest_row[0] else None

            # Distinct stocks in daily_bars
            stock_distinct_row = cur.execute("SELECT COUNT(DISTINCT ts_code) FROM daily_bars").fetchone()
            distinct_stocks = stock_distinct_row[0] if stock_distinct_row else 0

            return {
                "status": "success",
                "data": {
                    "daily_bars": daily_count,
                    "minute_bars": minute_count,
                    "trade_calendar": calendar_count,
                    "fundamentals": fundamentals_count,
                    "stock_info": stock_count,
                    "distinct_stocks": distinct_stocks,
                    "latest_trade_date": latest_date,
                    "last_updated": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }
            }
        finally:
            db.close()
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})


@router.post("/api/data/sync")
async def sync_data():
    """Sync market data from JSON cache to SQLite database"""
    try:
        from data.database import SQLiteManager
        db = SQLiteManager()
        try:
            counts = db.sync_from_cache()
            return {
                "status": "success",
                "data": {
                    "message": f"已同步 {counts['files']} 个缓存文件",
                    "daily_bars_inserted": counts['daily_bars'],
                    "stock_info_inserted": counts['stock_info'],
                    "files_processed": counts['files'],
                }
            }
        finally:
            db.close()
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})


# ============================================================
# Account / Dashboard helpers
# ============================================================

@router.get("/api/account")
async def get_account():
    """Get live account info for dashboard"""
    ls = _get_live_server()
    if ls is None:
        return JSONResponse(content={
            "status": "success",
            "data": {
                "total_assets": 0, "cash": 0, "market_value": 0,
                "profit_rate": 0, "daily_pnl": 0, "position_count": 0,
                "positions": [],
                "message": "Live server not started",
            }
        })

    try:
        status = ls.get_status()
        positions_raw = ls.get_positions()

        # Convert positions to a friendlier format
        positions_list = []
        for p in positions_raw:
            code = p.get('ts_code', '')
            qty = p.get('quantity', 0)
            cost = p.get('cost_price', 0)
            cur_price = p.get('current_price', 0)
            mv = p.get('market_value', cur_price * qty)
            pnl = p.get('profit', (cur_price - cost) * qty)
            pnl_pct = p.get('profit_rate', (cur_price / cost - 1) * 100 if cost > 0 else 0)

            # pnl_pct might already be float (e.g. 0.05), or already in percentage
            if isinstance(pnl_pct, float) and abs(pnl_pct) < 1:
                pnl_pct = pnl_pct * 100

            positions_list.append({
                'code': code,
                'name': p.get('name', code),
                'qty': qty,
                'cost': cost,
                'price': cur_price,
                'market_value': mv,
                'pnl': pnl,
                'pnl_pct': round(pnl_pct, 2),
            })

        acct = status.get('account', {})
        return {
            "status": "success",
            "data": {
                "total_assets": acct.get('total_assets', 0),
                "cash": acct.get('available_cash', 0),
                "market_value": acct.get('market_value', 0),
                "profit_rate": acct.get('total_profit_rate', 0) * 100 if isinstance(acct.get('total_profit_rate'), float) and abs(acct.get('total_profit_rate', 0)) < 1 else acct.get('total_profit_rate', 0),
                "daily_pnl": acct.get('daily_profit', 0),
                "position_count": acct.get('position_count', 0),
                "positions": positions_list,
                "broker_name": status.get('broker_label', ''),
                "trade_mode": status.get('trade_mode_label', ''),
                "running": status.get('running', False),
            }
        }
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)})

"""
回放测试 API — 多日录制 + 多日回放 + 策略多选 + 告警查询
"""

from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel
from typing import Optional
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.logger import get_logger

logger = get_logger('test', 'test.log')

router = APIRouter(prefix="/api/test", tags=["test"])

# 全局单例
_recorder = None
_replay_engine = None
_replay_thread = None


def get_recorder():
    """获取全局 RecordSession 单例（别名，兼容旧调用）。"""
    from test.replay_recorder import RecordSession
    return RecordSession.get_instance()


# ============================================================
# 录制
# ============================================================

class RecordStartRequest(BaseModel):
    date: str = ''
    stock_pool: Optional[list] = None


@router.post("/record/start")
def record_start(req: RecordStartRequest):
    """开始录制实盘数据流。"""
    recorder = get_recorder()
    if recorder.is_recording:
        return {'status': 'already_recording', 'dir': recorder._session_dir}
    stock_pool = req.stock_pool
    if not stock_pool:
        from config.settings import LIVE_TRADING_CONFIG
        stock_pool = LIVE_TRADING_CONFIG.get('scan', {}).get('stock_pool', [])
    return recorder.start(stock_pool, req.date)


@router.post("/record/stop")
def record_stop():
    """停止录制。"""
    recorder = get_recorder()
    return recorder.stop()


@router.get("/record/status")
def record_status():
    """查询录制状态。"""
    recorder = get_recorder()
    return {
        'is_recording': recorder.is_recording,
        'date': recorder.date,
        'snapshot_count': recorder.snapshot_count,
    }


@router.get("/record/sessions")
def record_sessions():
    """列出所有录制会话。"""
    from test.replay_recorder import RecordSession
    return RecordSession.list_sessions()


# ============================================================
# 策略列表
# ============================================================

@router.get("/strategies")
def list_strategies():
    """返回可选策略列表。"""
    from strategy import STRATEGY_REGISTRY
    return [
        {
            'name': name,
            'display': info.get('name', name),
            'description': info.get('description', ''),
            'category': info.get('category', ''),
        }
        for name, info in STRATEGY_REGISTRY.items()
    ]


# ============================================================
# 回放
# ============================================================

class ReplayStartRequest(BaseModel):
    sessions: list         # ['20260623', '20260624', ...]
    strategies: list       # ['momentum', 'eight_factor', ...]
    speed: float = 1.0
    initial_capital: float = 1_000_000
    commission_rate: float = 0.0003
    slippage_rate: float = 0.002
    stop_loss_rate: float = -0.08
    min_commission: float = 5.0
    max_positions: int = 10


@router.post("/replay/start")
def replay_start(req: ReplayStartRequest):
    """启动多日回放（后台线程执行）。"""
    global _replay_engine, _replay_thread

    if _replay_engine is not None and _replay_engine.is_running:
        return {'status': 'already_running'}

    if not req.sessions:
        raise HTTPException(status_code=400, detail='请选择至少一个会话')
    if not req.strategies:
        raise HTTPException(status_code=400, detail='请选择至少一个策略')

    from test.replay_engine import ReplayEngine

    _replay_engine = ReplayEngine(req.sessions)

    def _run():
        _replay_engine.run(
            strategies=req.strategies,
            speed=req.speed,
            initial_capital=req.initial_capital,
            commission_rate=req.commission_rate,
            slippage_rate=req.slippage_rate,
            stop_loss_rate=req.stop_loss_rate,
            min_commission=req.min_commission,
            max_positions=req.max_positions,
        )

    _replay_thread = threading.Thread(target=_run, daemon=True, name='replay-worker')
    _replay_thread.start()

    return {
        'status': 'started',
        'sessions': req.sessions,
        'strategies': req.strategies,
        'speed': req.speed,
    }


@router.post("/replay/stop")
def replay_stop():
    """停止回放。"""
    global _replay_engine
    if _replay_engine is None:
        return {'status': 'not_running'}
    result = _replay_engine.stop()
    return {'status': 'stopped', **result}


@router.post("/replay/speed")
def replay_speed(speed: float = Query(ge=0.1, le=3600)):
    """调整回放速度。"""
    if _replay_engine is None:
        raise HTTPException(status_code=400, detail='回放未启动')
    return _replay_engine.set_speed(speed)


@router.get("/replay/progress")
def replay_progress():
    """查询回放进度。"""
    if _replay_engine is None:
        return {'running': False, 'progress': 0}
    return _replay_engine.get_progress()


@router.get("/replay/results")
def replay_results():
    """获取完整回放结果（告警+策略信号+交易记录+绩效）。"""
    if _replay_engine is None:
        return {'status': 'not_run'}
    return _replay_engine.get_results()


@router.get("/replay/alerts")
def replay_alerts(limit: int = 500):
    """查询回放告警。"""
    if _replay_engine is None:
        return []
    return _replay_engine.get_alerts(limit)


@router.get("/replay/summary")
def replay_summary():
    """告警类型汇总。"""
    if _replay_engine is None:
        return {}
    return _replay_engine.get_alert_summary()

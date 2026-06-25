"""
回放测试 API — 录制控制 + 回放控制 + 结果查询
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


def get_recorder():
    global _recorder
    if _recorder is None:
        from test.replay_recorder import RecordSession
        _recorder = RecordSession()
    return _recorder


def get_replay_engine(session_date: str):
    global _replay_engine
    if _replay_engine is not None and _replay_engine._session_date == session_date:
        return _replay_engine
    from test.replay_engine import ReplayEngine
    _replay_engine = ReplayEngine(session_date)
    return _replay_engine


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
# 回放
# ============================================================

class ReplayStartRequest(BaseModel):
    session_date: str
    speed: float = 1.0
    mode: str = 'monitor'  # 'monitor' | 'live' | 'both'


@router.post("/replay/start")
def replay_start(req: ReplayStartRequest):
    """启动回放。"""
    engine = get_replay_engine(req.session_date)
    return engine.start(speed=req.speed, mode=req.mode)


@router.post("/replay/stop")
def replay_stop():
    """停止回放。"""
    if _replay_engine is None:
        return {'status': 'not_running'}
    return _replay_engine.stop()


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


@router.get("/replay/alerts")
def replay_alerts(limit: int = 200):
    """查询回放产生的告警。"""
    if _replay_engine is None:
        return []
    return _replay_engine.get_alerts(limit)


@router.get("/replay/summary")
def replay_summary():
    """告警模块汇总。"""
    if _replay_engine is None:
        return {}
    return _replay_engine.get_alert_summary()

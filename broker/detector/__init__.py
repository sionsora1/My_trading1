"""
异动检测模块 — 6 个检测器 + AnomalyAlert
"""

from __future__ import annotations

import dataclasses
import json
import queue
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════
# AnomalyAlert — 新告警值对象
# ═══════════════════════════════════════════════════════════════


@dataclasses.dataclass
class AnomalyAlert:
    """灵活的异动告警值对象。

    字段:
        type:     告警大类 'trans_big' | 'orderbook' | 'divergence'
                   | 'turnover' | 'limit_move' | 'auction' | 'rank_change'
        subtype:  子类型 'super_large' | 'imbalance_severe' | 'seal_loosen' 等
        code:     股票代码 '600519'
        name:     股票名称 '贵州茅台'
        direction:'buy' | 'sell' | 'neutral'
        time:     数据时间 '14:32:15'
        data:     自由格式 dict, 存放类型专属字段
    """

    type: str
    subtype: str
    code: str
    name: str
    direction: str = 'neutral'
    time: str = ''
    data: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_sse_data(self) -> str:
        """序列化为 SSE data 字段 (JSON 字符串)。"""
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, default=str)


# ═══════════════════════════════════════════════════════════════
# CooldownMixin — 所有检测器共享的冷却逻辑
# ═══════════════════════════════════════════════════════════════


class CooldownMixin:
    """为检测器提供统一的冷却期检查。

    子类需定义:
        self._cooldowns: Dict[str, float]  — key="code:subtype", value=timestamp
        self.cooldown_sec: float            — 冷却秒数
    """

    _cooldowns: Dict[str, float]
    cooldown_sec: float

    def _acquire_cooldown(self, code: str, subtype: str, now: float) -> bool:
        """检查并设置冷却期。

        如果距离上次该股票该子类型的告警在冷却期内，返回 False；
        否则更新时间戳并返回 True。
        """
        key = f"{code}:{subtype}"
        last = self._cooldowns.get(key, 0.0)
        if now - last < self.cooldown_sec:
            return False
        self._cooldowns[key] = now
        return True



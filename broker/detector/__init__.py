"""
异动检测模块 — 6 个检测器 + AnomalyAlert + SimpleQueue

检测器列表:
    DivergenceDetector  — 内外盘背离
    OrderbookDetector   — 盘口异动 (四维)
    LimitMoveDetector   — 涨跌停加速
    TurnoverDetector    — 换手率异动
    TransBigDetector    — 逐笔大单 (独立线程)
    RankChangeDetector  — 涨跌幅排名突变
"""

from __future__ import annotations

import dataclasses
import json
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
# SimpleQueue — 线程安全非阻塞队列
# ═══════════════════════════════════════════════════════════════


class SimpleQueue:
    """线程安全非阻塞队列，用于逐笔大单跨线程传递告警。"""

    def __init__(self):
        self._items: deque = deque()
        self._lock = threading.Lock()

    def put(self, alert: AnomalyAlert) -> None:
        with self._lock:
            self._items.append(alert)

    def get_all_nonblocking(self) -> List[AnomalyAlert]:
        """取出所有待处理项 (非阻塞)。"""
        with self._lock:
            items = list(self._items)
            self._items.clear()
            return items

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._items)

"""
信号通知系统
支持 Web 推送、文件记录、控制台打印三种方式
"""

import json
import os
from datetime import datetime
from typing import List, Dict
from dataclasses import asdict

from .base import Signal


class SignalNotifier:
    """
    信号通知器

    三种通知方式：
    1. 文件记录 → signals.json 持久化
    2. 控制台打印 → 彩色信号卡片
    3. Web 轮询 → 格式化的信号列表供前端读取
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.data_dir = self.config.get('data_dir', './data_cache')
        self.signals_file = os.path.join(self.data_dir, 'signals.json')

        # 信号存储
        self.signals: Dict[str, Signal] = {}  # key = ts_code + strategy
        self.signal_history: List[dict] = []

        # 加载已有信号
        self._load_signals()

    def add_signal(self, signal: Signal):
        """添加信号"""
        key = f"{signal.ts_code}_{signal.strategy}_{signal.signal}"
        signal.create_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.signals[key] = signal
        self._save_signals()

    def add_signals(self, signals: List[Signal]):
        """批量添加信号"""
        for signal in signals:
            self.add_signal(signal)

    def confirm_signal(self, ts_code: str, strategy: str, signal_type: str,
                       confirmed: bool = True) -> bool:
        """确认/拒绝信号"""
        key = f"{ts_code}_{strategy}_{signal_type}"
        if key in self.signals:
            self.signals[key].confirmed = confirmed
            if not confirmed:
                # 拒绝时加入历史
                s = self.signals[key]
                self.signal_history.append({
                    'ts_code': s.ts_code,
                    'name': s.name,
                    'signal': s.signal,
                    'reason': s.reason,
                    'strategy': s.strategy,
                    'confirmed': False,
                    'executed': False,
                    'time': s.create_time,
                    'action_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                })
                del self.signals[key]
            self._save_signals()
            return True
        return False

    def mark_executed(self, ts_code: str, strategy: str, signal_type: str,
                      order_id: str = ''):
        """标记信号已执行"""
        key = f"{ts_code}_{strategy}_{signal_type}"
        if key in self.signals:
            s = self.signals[key]
            s.executed = True
            s.order_id = order_id
            self.signal_history.append({
                'ts_code': s.ts_code,
                'name': s.name,
                'signal': s.signal,
                'reason': s.reason,
                'strategy': s.strategy,
                'confirmed': s.confirmed,
                'executed': True,
                'order_id': order_id,
                'time': s.create_time,
                'action_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            })
            del self.signals[key]
            self._save_signals()

    def get_pending_signals(self) -> List[dict]:
        """获取待处理的信号（用于 Web 展示）"""
        result = []
        for key, s in sorted(self.signals.items()):
            result.append({
                'ts_code': s.ts_code,
                'name': s.name,
                'signal': s.signal,
                'weight': s.weight,
                'reason': s.reason,
                'price': s.price,
                'strategy': s.strategy,
                'create_time': s.create_time,
                'confirmed': s.confirmed,
                'executed': s.executed,
            })
        return result

    def get_all_signals(self) -> List[dict]:
        """获取所有信号（待处理 + 历史）"""
        pending = self.get_pending_signals()
        return pending + self.signal_history

    def get_signal_stats(self) -> dict:
        """获取信号统计"""
        pending = len(self.signals)
        buy_count = sum(1 for s in self.signals.values() if s.signal == 'BUY')
        sell_count = sum(1 for s in self.signals.values() if s.signal == 'SELL')
        confirmed_count = sum(1 for s in self.signals.values() if s.confirmed)
        return {
            'pending': pending,
            'buy_signals': buy_count,
            'sell_signals': sell_count,
            'hold_signals': pending - buy_count - sell_count,
            'confirmed': confirmed_count,
            'unconfirmed': pending - confirmed_count,
            'total_history': len(self.signal_history),
        }

    def clear_pending(self):
        """清空待处理信号"""
        self.signals.clear()
        self._save_signals()

    def print_signals(self):
        """在控制台打印信号卡片"""
        if not self.signals:
            print("\n[Signal] No trading signals")
            return

        print("\n" + "=" * 60)
        print(f"[Signal] Trading Signals | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {len(self.signals)} total")
        print("=" * 60)

        for key, s in sorted(self.signals.items()):
            action = "SELL" if s.signal == 'SELL' else "BUY" if s.signal == 'BUY' else "HOLD"
            status = "[CONFIRMED]" if s.confirmed else "[PENDING]"

            print(f"\n  [{action}] {s.ts_code} {s.name} {status}")
            print(f"     Price: {s.price:.2f} | Strategy: {s.strategy}")
            print(f"     Reason: {s.reason}")

        print("\n" + "=" * 60)

    def _save_signals(self):
        """保存信号到文件"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)

            pending_data = []
            for key, s in self.signals.items():
                pending_data.append({
                    'ts_code': s.ts_code,
                    'name': s.name,
                    'signal': s.signal,
                    'weight': s.weight,
                    'reason': s.reason,
                    'price': s.price,
                    'strategy': s.strategy,
                    'create_time': s.create_time,
                    'confirmed': s.confirmed,
                    'executed': s.executed,
                })

            data = {
                'pending': pending_data,
                'history': self.signal_history[-200:],  # 保留最近200条
                'stats': self.get_signal_stats(),
                'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }

            with open(self.signals_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

        except Exception as e:
            print(f"[通知] 保存信号失败: {e}")

    def _load_signals(self):
        """从文件加载信号"""
        try:
            if not os.path.exists(self.signals_file):
                return

            with open(self.signals_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            for s in data.get('pending', []):
                key = f"{s['ts_code']}_{s['strategy']}_{s['signal']}"
                self.signals[key] = Signal(
                    ts_code=s['ts_code'],
                    name=s.get('name', ''),
                    signal=s['signal'],
                    weight=s.get('weight', 0),
                    reason=s.get('reason', ''),
                    price=s.get('price', 0),
                    strategy=s.get('strategy', ''),
                    create_time=s.get('create_time', ''),
                    confirmed=s.get('confirmed', False),
                    executed=s.get('executed', False),
                    order_id=s.get('order_id', ''),
                )

            self.signal_history = data.get('history', [])

        except Exception:
            pass

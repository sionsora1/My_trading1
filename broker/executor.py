"""
交易执行清单
生成清晰的执行计划，适合手机APP手动执行场景
"""

import json
import os
from datetime import datetime
from typing import List, Dict, Optional


class TradeChecklist:
    """
    交易执行清单

    工作流：
    1. 策略扫描 → 生成信号
    2. 信号转为执行清单
    3. 用户在APP逐条执行
    4. 回到系统标记完成/跳过
    5. 系统自动更新持仓
    """

    def __init__(self, data_dir: str = './data_cache'):
        self.data_dir = data_dir
        self.checklist_file = os.path.join(data_dir, 'trade_checklist.json')
        self.history_file = os.path.join(data_dir, 'trade_history.json')

        self.items: List[dict] = []
        self.history: List[dict] = []
        self._load()

    def generate(self, signals: List[dict], account: dict) -> List[dict]:
        """
        从信号生成执行清单

        Args:
            signals: [{'ts_code', 'name', 'signal', 'price', 'reason', 'strategy', 'weight'}]
            account: {'total_assets', 'available_cash', 'market_value'}

        Returns:
            执行清单列表
        """
        self.items = []
        for i, sig in enumerate(signals):
            price = sig.get('price', 0)
            weight = sig.get('weight', 0.20)
            total_assets = account.get('total_assets', 100000)

            if sig['signal'] == 'BUY':
                amount = total_assets * weight
                qty = int(amount / price / 100) * 100 if price > 0 else 100
                qty = max(qty, 100)
                note = f"建议买入{qty}股（约{amount:,.0f}元，仓位{weight:.0%}）"
            elif sig['signal'] == 'SELL':
                qty = 0  # 全仓卖出由用户自己决定数量
                note = "建议全部卖出"
            else:
                continue

            item = {
                'id': f"CK{i+1:03d}",
                'ts_code': sig['ts_code'],
                'name': sig.get('name', ''),
                'action': '买入' if sig['signal'] == 'BUY' else '卖出',
                'signal': sig['signal'],
                'suggested_price': round(price, 2),
                'suggested_quantity': qty,
                'reason': sig.get('reason', ''),
                'strategy': sig.get('strategy', ''),
                'weight': weight,
                'note': note,
                'status': 'pending',  # pending / executed / skipped
                'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'executed_at': '',
                'actual_price': 0,
                'actual_quantity': 0,
                'actual_amount': 0,
            }
            self.items.append(item)

        self._save()
        return self.items

    def mark_executed(self, item_id: str, actual_price: float = 0,
                      actual_quantity: int = 0) -> Optional[dict]:
        """标记已执行"""
        for item in self.items:
            if item['id'] == item_id:
                item['status'] = 'executed'
                item['executed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                item['actual_price'] = actual_price or item['suggested_price']
                item['actual_quantity'] = actual_quantity or item['suggested_quantity']
                item['actual_amount'] = item['actual_price'] * item['actual_quantity']

                # 加入历史
                self.history.append(dict(item))
                self._save()
                return item
        return None

    def mark_skipped(self, item_id: str) -> Optional[dict]:
        """标记跳过"""
        for item in self.items:
            if item['id'] == item_id:
                item['status'] = 'skipped'
                item['executed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self._save()
                return item
        return None

    def get_pending(self) -> List[dict]:
        """获取待执行清单"""
        return [i for i in self.items if i['status'] == 'pending']

    def get_all(self) -> List[dict]:
        """获取全部清单"""
        return self.items

    def get_summary(self) -> dict:
        """获取摘要"""
        total = len(self.items)
        executed = sum(1 for i in self.items if i['status'] == 'executed')
        skipped = sum(1 for i in self.items if i['status'] == 'skipped')
        pending = total - executed - skipped
        buy_items = sum(1 for i in self.items if i['action'] == '买入')
        sell_items = sum(1 for i in self.items if i['action'] == '卖出')

        total_buy_amount = sum(
            i['actual_amount'] for i in self.items
            if i['status'] == 'executed' and i['action'] == '买入'
        )
        total_sell_amount = sum(
            i['actual_amount'] for i in self.items
            if i['status'] == 'executed' and i['action'] == '卖出'
        )

        return {
            'total': total,
            'pending': pending,
            'executed': executed,
            'skipped': skipped,
            'buy_items': buy_items,
            'sell_items': sell_items,
            'total_buy_amount': total_buy_amount,
            'total_sell_amount': total_sell_amount,
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }

    def clear(self):
        """清空清单"""
        # 把未完成的移入历史
        for item in self.items:
            if item['status'] == 'pending':
                item['status'] = 'expired'
                item['executed_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                self.history.append(dict(item))
        self.items = []
        self._save()

    def get_history(self, limit: int = 50) -> List[dict]:
        """获取历史记录"""
        return sorted(self.history, key=lambda x: x.get('created_at', ''), reverse=True)[:limit]

    def print_checklist(self):
        """打印清单到控制台"""
        import sys
        summary = self.get_summary()
        pending = self.get_pending()

        def safe_print(msg):
            """安全打印，避免 Windows GBK 编码报错"""
            try:
                print(msg)
            except UnicodeEncodeError:
                print(msg.encode('ascii', errors='replace').decode('ascii'))

        safe_print("\n" + "=" * 60)
        safe_print("[交易执行清单]")
        safe_print(f"   待执行: {summary['pending']} | 买入: {summary['buy_items']} | 卖出: {summary['sell_items']}")
        safe_print("=" * 60)

        if not pending:
            safe_print("\n   [OK] 所有任务已完成")
            return

        for item in pending:
            icon = "[BUY]" if item['action'] == '买入' else "[SELL]"
            safe_print(f"\n  {icon} {item['id']}: {item['ts_code']} {item['name']}")
            safe_print(f"     {item['action']} {item['suggested_quantity']}股 @ {item['suggested_price']}")
            safe_print(f"     Reason: {item['reason']}")
            safe_print(f"     Strategy: {item['strategy']}")

        safe_print("\n" + "=" * 60)
        safe_print("  请在东方财富APP中执行上述操作，完成后回到系统标记")
        safe_print("=" * 60)

    def _save(self):
        """保存到文件"""
        os.makedirs(self.data_dir, exist_ok=True)
        data = {
            'items': self.items,
            'history': self.history[-200:],
            'summary': self.get_summary(),
            'last_save': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        with open(self.checklist_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)

    def _load(self):
        """从文件加载"""
        try:
            if os.path.exists(self.checklist_file):
                with open(self.checklist_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.items = data.get('items', [])
                self.history = data.get('history', [])
        except Exception:
            pass

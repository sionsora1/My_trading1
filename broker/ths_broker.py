"""
同花顺（THS）券商连接器

通过 32位 Python 桥接服务（broker/ths_bridge_server.py）操控同花顺 PC 客户端。
架构：64位主程序 --HTTP--> 32位桥接服务 --easytrader--> 同花顺 PC 客户端

前置条件：
1. 安装 32 位 Python (C:\Python310_x86)
2. 在 32 位 Python 中安装依赖：pip install easytrader pywin32
3. 启动桥接服务：C:\Python310_x86\python.exe broker/ths_bridge_server.py
4. 同花顺 PC 客户端保持运行并登录交易
"""

import json
import time
from datetime import datetime
from typing import Dict, List, Optional

import requests

from .base import (
    BaseBroker, OrderRequest, OrderResult, OrderSide,
    OrderType, OrderStatus, AccountInfo, PositionInfo
)


class THSBroker(BaseBroker):
    """
    同花顺券商连接器（通过 32位 Python 桥接服务）

    从 easytrader UI 自动化方案改为 HTTP 桥接方案，
    解决 32/64 位兼容性问题。
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.name = '同花顺'
        self._bridge_url = self.config.get('bridge_url', 'http://127.0.0.1:18888')
        self._session = requests.Session()
        self._session.timeout = 30
        self._balance_cache = {}
        self._position_cache = {}
        self._last_balance_time = 0
        self._last_position_time = 0
        self._cache_ttl = 5

    def _call(self, method: str, endpoint: str, data: dict = None) -> dict:
        """调用桥接服务 API"""
        try:
            url = f"{self._bridge_url}{endpoint}"
            if method == 'GET':
                resp = self._session.get(url, timeout=10)
            else:
                resp = self._session.post(url, json=data or {}, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            else:
                return {"success": False, "message": f"HTTP {resp.status_code}"}
        except requests.exceptions.ConnectionError:
            return {"success": False, "message": "Bridge server not running. Start: C:\\Python310_x86\\python.exe broker/ths_bridge_server.py"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    # ============================================================
    # 连接
    # ============================================================

    def connect(self) -> bool:
        """
        连接同花顺（通过桥接服务）
        """
        result = self._call('POST', '/connect')
        if result.get('success'):
            self.connected = True
            print(f"[同花顺] 连接成功")
            return True
        else:
            msg = result.get('message', 'Unknown error')
            print(f"[同花顺] 连接失败: {msg}")
            print("[同花顺] 请确认：")
            print("  1. 桥接服务已启动（C:\\Python310_x86\\python.exe broker/ths_bridge_server.py）")
            print("  2. 同花顺 PC 客户端已打开并登录")
            print("  3. 交易界面已解锁（输入了交易密码）")
            return False

    def disconnect(self) -> bool:
        self._call('POST', '/disconnect')
        self.connected = False
        self._balance_cache = {}
        self._position_cache = {}
        return True

    def _ensure_connected(self) -> bool:
        if not self.connected:
            return self.connect()
        # 检查桥接服务是否还在
        health = self._call('GET', '/health')
        if not health.get('success') or not health.get('connected'):
            return self.connect()
        return True

    # ============================================================
    # 账户
    # ============================================================

    def get_account(self) -> AccountInfo:
        if not self._ensure_connected():
            return AccountInfo(broker_name='同花顺(未连接)')

        try:
            result = self._call('GET', '/account')
            if not result.get('success'):
                return AccountInfo(broker_name='同花顺')

            balance = result.get('data', {})
            if not balance or not isinstance(balance, dict):
                return AccountInfo(broker_name='同花顺')

            total_assets = self._parse_float(balance, ['总资产', '总市值', 'asset', 'total_asset'])
            available = self._parse_float(balance, ['可用资金', '可用金额', '可用', 'available', 'enable_balance'])
            frozen = self._parse_float(balance, ['冻结资金', '冻结金额', '冻结', 'frozen'])
            market_value = self._parse_float(balance, ['股票市值', '市值', 'market_value'])

            if total_assets == 0 and available > 0:
                total_assets = available + (frozen or 0) + (market_value or 0)

            positions = self.get_positions()

            return AccountInfo(
                broker_name='同花顺',
                account_id='THS',
                total_assets=total_assets,
                available_cash=available,
                frozen_cash=frozen or 0,
                market_value=market_value or 0,
                total_profit=0,
                total_profit_rate=0,
                position_count=len(positions),
                update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
        except Exception as e:
            print(f"[同花顺] 获取账户失败: {e}")
            return AccountInfo(broker_name='同花顺(异常)')

    def get_positions(self) -> Dict[str, PositionInfo]:
        positions = {}
        if not self._ensure_connected():
            return positions

        try:
            result = self._call('GET', '/positions')
            if not result.get('success'):
                return positions

            raw = result.get('data', [])
            if not isinstance(raw, list):
                return positions

            for item in raw:
                if not isinstance(item, dict):
                    continue

                code = str(item.get('证券代码', item.get('stock_code', item.get('代码', ''))))
                if not code or len(code) < 6:
                    continue

                if code.startswith(('SH', 'SZ', 'BJ')):
                    code = code[2:]
                if '.' in code:
                    code = code.split('.')[0]

                name = item.get('证券名称', item.get('stock_name', item.get('名称', code)))
                qty = self._parse_int(item, ['股票余额', '当前拥股', 'volume', 'amount', 'current_amount'])
                avail = self._parse_int(item, ['可用余额', '可用股份', 'enable_amount', 'sellable'])
                cost = self._parse_float(item, ['成本价', '买入均价', 'cost_price', 'price'])
                cur_price = self._parse_float(item, ['市价', '现价', '当前价', 'current_price', 'last_price'])
                mkt_val = self._parse_float(item, ['市值', 'market_value'])
                pnl = self._parse_float(item, ['盈亏', '浮动盈亏', 'profit', 'float_profit'])
                pnl_rate = self._parse_float(item, ['盈亏比例', '盈亏率(%)', 'profit_rate', 'profit_ratio'])

                if mkt_val == 0 and qty > 0 and cur_price > 0:
                    mkt_val = qty * cur_price
                if pnl == 0 and qty > 0 and cost > 0 and cur_price > 0:
                    pnl = (cur_price - cost) * qty
                if pnl_rate == 0 and cost > 0 and cur_price > 0:
                    pnl_rate = (cur_price / cost - 1)

                positions[code] = PositionInfo(
                    ts_code=code,
                    name=name,
                    quantity=qty,
                    available_quantity=avail if avail > 0 else qty,
                    cost_price=cost,
                    current_price=cur_price,
                    market_value=mkt_val,
                    profit=pnl,
                    profit_rate=pnl_rate,
                    entry_date='',
                )

        except Exception as e:
            print(f"[同花顺] 获取持仓失败: {e}")

        return positions

    # ============================================================
    # 下单
    # ============================================================

    def submit_order(self, request: OrderRequest) -> OrderResult:
        if not self._ensure_connected():
            return OrderResult(
                order_id='',
                ts_code=request.ts_code,
                side=request.side,
                price=request.price,
                quantity=request.quantity,
                status=OrderStatus.REJECTED,
                error_message='同花顺未连接',
                create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )

        try:
            code = request.ts_code
            price = request.price if request.price > 0 else 0
            qty = request.quantity

            if request.side == OrderSide.BUY:
                result = self._call('POST', '/buy', {'code': code, 'price': price, 'qty': qty})
            else:
                result = self._call('POST', '/sell', {'code': code, 'price': price, 'qty': qty})

            if result.get('success'):
                data = result.get('data', {})
                order_id = ''
                if isinstance(data, dict):
                    order_id = str(data.get('entrust_no', data.get('合同编号', '')))
                elif isinstance(data, str):
                    order_id = data

                # 等待一下让同花顺处理
                time.sleep(0.5)

                return OrderResult(
                    order_id=order_id,
                    ts_code=request.ts_code,
                    side=request.side,
                    price=price,
                    quantity=qty,
                    status=OrderStatus.PENDING,
                    create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    reason=request.reason,
                )
            else:
                return OrderResult(
                    order_id='',
                    ts_code=request.ts_code,
                    side=request.side,
                    price=request.price,
                    quantity=request.quantity,
                    status=OrderStatus.REJECTED,
                    error_message=result.get('message', '下单失败'),
                    create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                )

        except Exception as e:
            return OrderResult(
                order_id='',
                ts_code=request.ts_code,
                side=request.side,
                price=request.price,
                quantity=request.quantity,
                status=OrderStatus.REJECTED,
                error_message=f'同花顺下单失败: {e}',
                create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )

    def cancel_order(self, order_id: str) -> bool:
        if not self._ensure_connected():
            return False
        try:
            result = self._call('POST', '/cancel', {'order_id': order_id})
            return result.get('success', False)
        except Exception as e:
            print(f"[同花顺] 撤单失败: {e}")
            return False

    def get_orders(self, status: OrderStatus = None, limit: int = 50) -> List[OrderResult]:
        orders = []
        if not self._ensure_connected():
            return orders

        try:
            result = self._call('GET', '/orders')
            if not result.get('success'):
                return orders

            raw_orders = result.get('data', [])
            if not isinstance(raw_orders, list):
                return orders

            for o in raw_orders[:limit]:
                if not isinstance(o, dict):
                    continue

                code = str(o.get('证券代码', o.get('stock_code', '')))
                if code.startswith(('SH', 'SZ')):
                    code = code[2:]
                if '.' in code:
                    code = code.split('.')[0]

                bs = str(o.get('买卖方向', o.get('操作', o.get('trade_type', ''))))
                if '买' in bs:
                    side = OrderSide.BUY
                elif '卖' in bs:
                    side = OrderSide.SELL
                else:
                    side = OrderSide.BUY

                entrust_status = str(o.get('状态', o.get('entrust_status', o.get('status', ''))))
                order_status = self._map_ths_status(entrust_status)
                if status and order_status != status:
                    continue

                price = self._parse_float(o, ['委托价格', 'price', 'entrust_price'])
                qty = self._parse_int(o, ['委托数量', 'amount', 'entrust_amount'])
                filled_qty = self._parse_int(o, ['成交数量', 'business_amount', 'filled'])
                filled_price = self._parse_float(o, ['成交均价', 'business_price', 'filled_price'])

                orders.append(OrderResult(
                    order_id=str(o.get('合同编号', o.get('委托编号', o.get('entrust_no', '')))),
                    ts_code=code,
                    side=side,
                    price=price,
                    quantity=qty,
                    filled_quantity=filled_qty,
                    filled_price=filled_price,
                    status=order_status,
                    amount=filled_price * filled_qty if filled_price > 0 and filled_qty > 0 else price * qty,
                    create_time=str(o.get('委托时间', o.get('time', ''))),
                    update_time=str(o.get('更新时间', o.get('update_time', ''))),
                ))

        except Exception as e:
            print(f"[同花顺] 获取订单失败: {e}")

        return orders

    def get_order(self, order_id: str) -> Optional[OrderResult]:
        orders = self.get_orders()
        for o in orders:
            if o.order_id == order_id:
                return o
        return None

    # ============================================================
    # 实时行情
    # ============================================================

    def get_realtime_quote(self, ts_code: str) -> dict:
        if not self._ensure_connected():
            return {}

        try:
            result = self._call('GET', f'/quote/{ts_code}')
            if result.get('success'):
                data = result.get('data', {})
                if isinstance(data, dict):
                    return {
                        'ts_code': ts_code,
                        'last_price': data.get('现价', data.get('last_price', 0)),
                        'open': data.get('今开', data.get('open', 0)),
                        'high': data.get('最高', data.get('high', 0)),
                        'low': data.get('最低', data.get('low', 0)),
                        'volume': data.get('成交量', data.get('volume', 0)),
                        'amount': data.get('成交额', data.get('amount', 0)),
                        'bid1': data.get('买一', data.get('bid1', 0)),
                        'ask1': data.get('卖一', data.get('ask1', 0)),
                        'change_pct': data.get('涨跌幅', data.get('change_pct', 0)),
                    }
        except Exception:
            pass

        return {}

    # ============================================================
    # 内部方法
    # ============================================================

    def _parse_float(self, data: dict, keys: List[str]) -> float:
        for key in keys:
            val = data.get(key)
            if val is not None and val != '' and val != '--':
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return 0.0

    def _parse_int(self, data: dict, keys: List[str]) -> int:
        for key in keys:
            val = data.get(key)
            if val is not None and val != '' and val != '--':
                try:
                    return int(float(val))
                except (ValueError, TypeError):
                    continue
        return 0

    def _parse_success(self, result) -> bool:
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            msg = str(result.get('message', result.get('msg', '')))
            return '成功' in msg or '已' in msg
        if isinstance(result, str):
            return '成功' in result or len(result) > 0
        return False

    def _map_ths_status(self, status_str: str) -> OrderStatus:
        s = status_str.strip()
        if any(kw in s for kw in ['已成', '成交', '全部成交']):
            return OrderStatus.FILLED
        if any(kw in s for kw in ['部成', '部分成交', '部分成']):
            return OrderStatus.PARTIAL
        if any(kw in s for kw in ['已撤', '撤单', '已撤销', '废单']):
            return OrderStatus.CANCELLED
        if any(kw in s for kw in ['已报', '未报', '待报', '申报中', '已申报', '未成交']):
            return OrderStatus.PENDING
        if any(kw in s for kw in ['拒绝', '失败', '废单']):
            return OrderStatus.REJECTED
        return OrderStatus.PENDING

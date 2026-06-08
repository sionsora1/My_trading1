"""
同花顺（THS）券商连接器

通过 easytrader 库操控同花顺 PC 客户端实现自动交易。

前置条件：
1. 安装同花顺 PC 客户端（普及版/核新版均可），完成登录
2. pip install easytrader
3. 交易时段内客户端保持运行（可以最小化到托盘）

原理：
easytrader 通过 Windows 窗口消息找到同花顺的交易窗口，
模拟键盘输入完成买卖操作，属于 UI 自动化范畴。
不是网络 API，所以不需要任何 token/密钥。

限制：
- 同花顺客户端版本更新后 easytrader 可能暂时失效（需等库更新）
- 不能在锁屏/远程桌面断开状态下运行（需要图形界面）
- 交易速度受 UI 操作限制（约 2-5 秒/笔）
- 不支持同时运行多个同花顺客户端
"""

import time
from datetime import datetime
from typing import Dict, List, Optional

from .base import (
    BaseBroker, OrderRequest, OrderResult, OrderSide,
    OrderType, OrderStatus, AccountInfo, PositionInfo
)


class THSBroker(BaseBroker):
    """
    同花顺券商连接器（通过 easytrader 操控 PC 客户端）

    支持的券商：
    理论上支持所有同花顺 PC 客户端接入的券商（绝大多数券商都支持同花顺），
    包括但不限于：华泰、中信、国泰君安、海通、招商、广发等。
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.name = '同花顺'
        self._trader = None
        self._exe_path = self.config.get('exe_path', '')  # 同花顺客户端路径，留空自动查找
        self._connect_retries = self.config.get('connect_retries', 3)
        self._connect_delay = self.config.get('connect_delay', 2)  # 重试间隔秒数
        self._balance_cache = None
        self._position_cache = None
        self._last_balance_time = None
        self._cache_ttl = 5  # 缓存5秒，避免频繁读窗口

    # ============================================================
    # 连接
    # ============================================================

    def connect(self) -> bool:
        """
        连接到同花顺客户端

        步骤：
        1. 导入 easytrader
        2. 查找同花顺窗口
        3. 连接交易接口
        4. 验证连接状态
        """
        try:
            import easytrader

            for attempt in range(self._connect_retries):
                try:
                    self._trader = easytrader.use('ths')

                    # 如果指定了同花顺安装路径
                    if self._exe_path:
                        self._trader.connect(self._exe_path)
                    else:
                        self._trader.connect()

                    # 验证：尝试读取余额
                    balance = self._trader.balance
                    if balance and isinstance(balance, dict) and len(balance) > 0:
                        self.connected = True
                        self._balance_cache = balance
                        self._last_balance_time = time.time()
                        print(f"[同花顺] 连接成功，总资产: {balance.get('总资产', balance.get('总市值', 'N/A'))}")
                        return True

                except Exception as e:
                    print(f"[同花顺] 第{attempt+1}次连接失败: {e}")
                    if attempt < self._connect_retries - 1:
                        time.sleep(self._connect_delay)

            print("[同花顺] 所有重试均失败，请确认：")
            print("  1. 同花顺 PC 客户端已打开并登录")
            print("  2. 交易界面已解锁（输入了交易密码）")
            print("  3. pip install easytrader 已安装")
            return False

        except ImportError:
            print("[同花顺] 未安装 easytrader 库")
            print("[同花顺] 请运行: pip install easytrader")
            return False
        except Exception as e:
            print(f"[同花顺] 连接异常: {e}")
            return False

    def disconnect(self) -> bool:
        """断开连接（easytrader 实际不需要显式断开）"""
        self._trader = None
        self.connected = False
        self._balance_cache = None
        self._position_cache = None
        return True

    def _ensure_connected(self) -> bool:
        """确保连接有效，失败则尝试重连"""
        if not self.connected or self._trader is None:
            return self.connect()
        return True

    # ============================================================
    # 账户
    # ============================================================

    def get_account(self) -> AccountInfo:
        """获取账户信息"""
        if not self._ensure_connected():
            return AccountInfo(broker_name='同花顺(未连接)')

        try:
            balance = self._get_balance()
            if not balance:
                return AccountInfo(broker_name='同花顺')

            # easytrader balance 的 key 可能因版本不同而异，做兼容
            total_assets = self._parse_float(balance, ['总资产', '总市值', 'asset', 'total_asset'])
            available = self._parse_float(balance, ['可用资金', '可用', 'available', 'enable_balance'])
            frozen = self._parse_float(balance, ['冻结资金', '冻结', 'frozen'])
            market_value = self._parse_float(balance, ['市值', '股票市值', 'market_value'])
            # 有些版本总资产=可用+市值，有些需要从总资产减去
            if total_assets == 0 and available > 0:
                total_assets = available + (frozen or 0) + (market_value or 0)

            return AccountInfo(
                broker_name='同花顺',
                account_id='THS',
                total_assets=total_assets,
                available_cash=available,
                frozen_cash=frozen or 0,
                market_value=market_value or 0,
                total_profit=0,  # easytrader balance 通常不含累计盈亏
                total_profit_rate=0,
                position_count=len(self._get_positions_raw()),
                update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
        except Exception as e:
            print(f"[同花顺] 获取账户失败: {e}")
            return AccountInfo(broker_name='同花顺(异常)')

    def get_positions(self) -> Dict[str, PositionInfo]:
        """获取持仓列表"""
        positions = {}
        if not self._ensure_connected():
            return positions

        try:
            raw = self._get_positions_raw()
            for item in raw:
                if not isinstance(item, dict):
                    continue

                # 兼容不同的 key 命名
                code = str(item.get('证券代码', item.get('stock_code', item.get('代码', ''))))
                if not code or len(code) < 6:
                    continue

                # 去掉市场前缀（如 SH600519 → 600519）
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

                # 计算：如果市值和盈亏没有直接给，就自己算
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
        """提交订单"""
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

            if request.side == OrderSide.BUY:
                # easytrader buy(code, price, amount)
                # price=0 表示市价买入
                price = request.price if request.price > 0 else 0
                qty = request.quantity

                result_raw = self._trader.buy(code, price, qty)
            else:
                price = request.price if request.price > 0 else 0
                qty = request.quantity

                result_raw = self._trader.sell(code, price, qty)

            # 解析返回结果
            order_id = ''
            status = OrderStatus.PENDING
            error_msg = ''

            if isinstance(result_raw, dict):
                # easytrader 返回 {'entrust_no': 'xxx', ...} 或 {'message': '...'}
                order_id = str(result_raw.get('entrust_no', result_raw.get('合同编号', '')))
                msg = result_raw.get('message', result_raw.get('msg', ''))
                if '成功' in str(msg) or order_id:
                    status = OrderStatus.PENDING
                elif any(kw in str(msg) for kw in ['失败', '错误', '拒绝']):
                    status = OrderStatus.REJECTED
                    error_msg = msg
            elif isinstance(result_raw, str):
                order_id = result_raw
                status = OrderStatus.PENDING

            # 等待一下让同花顺处理
            time.sleep(0.5)

            result = OrderResult(
                order_id=order_id,
                ts_code=request.ts_code,
                side=request.side,
                price=price,
                quantity=qty,
                status=status,
                create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                reason=request.reason,
                error_message=error_msg,
            )

            if status == OrderStatus.REJECTED and not error_msg:
                result.error_message = f'下单异常: {result_raw}'

            return result

        except Exception as e:
            error_str = str(e)
            # easytrader 常见错误翻译
            if '买入失败' in error_str or '卖出失败' in error_str:
                pass  # 保持原消息
            return OrderResult(
                order_id='',
                ts_code=request.ts_code,
                side=request.side,
                price=request.price,
                quantity=request.quantity,
                status=OrderStatus.REJECTED,
                error_message=f'同花顺下单失败: {error_str}',
                create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单"""
        if not self._ensure_connected():
            return False
        try:
            # easytrader 的撤单方法
            if hasattr(self._trader, 'cancel_entrust'):
                result = self._trader.cancel_entrust(order_id)
                return self._parse_success(result)
            elif hasattr(self._trader, 'cancel'):
                result = self._trader.cancel(order_id)
                return self._parse_success(result)
            else:
                print("[同花顺] 当前 easytrader 版本不支持撤单")
                return False
        except Exception as e:
            print(f"[同花顺] 撤单失败: {e}")
            return False

    def get_orders(self, status: OrderStatus = None, limit: int = 50) -> List[OrderResult]:
        """获取今日订单列表"""
        orders = []
        if not self._ensure_connected():
            return orders

        try:
            # easytrader 的今日委托
            raw_orders = []
            if hasattr(self._trader, 'today_entrusts'):
                raw_orders = self._trader.today_entrusts or []
            elif hasattr(self._trader, 'entrust'):
                raw_orders = self._trader.entrust or []

            for o in raw_orders[:limit]:
                if not isinstance(o, dict):
                    continue

                code = str(o.get('证券代码', o.get('stock_code', '')))
                if code.startswith(('SH', 'SZ')):
                    code = code[2:]
                if '.' in code:
                    code = code.split('.')[0]

                # 方向
                bs = o.get('买卖方向', o.get('操作', o.get('trade_type', '')))
                if '买' in str(bs):
                    side = OrderSide.BUY
                elif '卖' in str(bs):
                    side = OrderSide.SELL
                else:
                    side = OrderSide.BUY

                # 状态
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
        """查询单个订单"""
        orders = self.get_orders()
        for o in orders:
            if o.order_id == order_id:
                return o
        return None

    # ============================================================
    # 实时行情（通过同花顺客户端）
    # ============================================================

    def get_realtime_quote(self, ts_code: str) -> dict:
        """获取实时行情"""
        if not self._ensure_connected():
            return {}

        try:
            # easytrader 支持获取实时行情
            if hasattr(self._trader, 'get_quote'):
                data = self._trader.get_quote(ts_code)
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

    def _get_balance(self) -> dict:
        """获取余额（带缓存）"""
        now = time.time()
        if (self._balance_cache and self._last_balance_time
                and (now - self._last_balance_time) < self._cache_ttl):
            return self._balance_cache

        if self._trader and hasattr(self._trader, 'balance'):
            raw = self._trader.balance
            if isinstance(raw, dict):
                self._balance_cache = raw
                self._last_balance_time = now
            elif isinstance(raw, list) and len(raw) > 0:
                self._balance_cache = raw[0] if isinstance(raw[0], dict) else {}
                self._last_balance_time = now
            else:
                self._balance_cache = {}

        return self._balance_cache or {}

    def _get_positions_raw(self) -> List[dict]:
        """获取原始持仓列表（带缓存）"""
        now = time.time()
        if self._position_cache is not None and (now - getattr(self, '_last_position_time', 0)) < self._cache_ttl:
            return self._position_cache

        positions = []
        if self._trader and hasattr(self._trader, 'position'):
            raw = self._trader.position
            if isinstance(raw, list):
                positions = [item for item in raw if isinstance(item, dict)]
            elif isinstance(raw, dict):
                # 有的版本返回 {code: {...}}
                positions = list(raw.values())

        self._position_cache = positions
        self.__dict__['_last_position_time'] = now
        return positions

    def _parse_float(self, data: dict, keys: List[str]) -> float:
        """从字典中尝试多个 key 解析浮点数"""
        for key in keys:
            val = data.get(key)
            if val is not None and val != '' and val != '--':
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
        return 0.0

    def _parse_int(self, data: dict, keys: List[str]) -> int:
        """从字典中尝试多个 key 解析整数"""
        for key in keys:
            val = data.get(key)
            if val is not None and val != '' and val != '--':
                try:
                    return int(float(val))
                except (ValueError, TypeError):
                    continue
        return 0

    def _parse_success(self, result) -> bool:
        """判断操作是否成功"""
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            msg = str(result.get('message', result.get('msg', '')))
            return '成功' in msg or '已' in msg
        if isinstance(result, str):
            return '成功' in result or len(result) > 0
        return False

    def _map_ths_status(self, status_str: str) -> OrderStatus:
        """将同花顺状态文字映射到 OrderStatus"""
        s = status_str.strip()
        if any(kw in s for kw in ['已成', '成交', '全部成交', '已成']):
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

"""
QMT（迅投）券商连接器

使用方法：
1. 安装迅投 QMT 交易端
2. 在 QMT 中启用 MiniQMT 模式
3. pip install xtquant
4. 修改本文件中的 account_id 和 data_dir

注意：xtquant 需要通过 QMT 交易端提供的链接下载，
不是 pip install xtquant 安装的公开版本。
"""

from datetime import datetime
from typing import Dict, List, Optional

from .base import (
    BaseBroker, OrderRequest, OrderResult, OrderSide,
    OrderType, OrderStatus, AccountInfo, PositionInfo
)


class QMTBroker(BaseBroker):
    """
    QMT（迅投 MiniQMT）券商连接器

    通过 xtquant 库连接真实券商账户。

    前置条件：
    - 安装 xtquant: pip install xtquant（或从 QMT 安装目录获取）
    - 启动 QMT 交易端并登录券商账户
    - 在 QMT 中启用 MiniQMT 模式（设置 → 模型交易 → 启用 MiniQMT）

    支持的券商：国金证券、华鑫证券、中泰证券、国信证券等
    （具体以各券商是否支持 QMT 为准，东方财富目前不直接支持 QMT）
    """

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.name = 'QMT'
        self.account_id = self.config.get('account_id', '')
        self.miniqmt_path = self.config.get('miniqmt_path', '')
        self.xt_trader = None
        self.xt_data = None

    def connect(self) -> bool:
        """
        连接到 QMT 交易端

        Returns:
            bool: 连接是否成功
        """
        try:
            import xtquant.xtdata as xtdata
            import xtquant.xttrader as xttrader

            path = self.miniqmt_path or r'D:\国金QMT交易端\userdata_mini'

            # 连接行情
            xtdata.connect()
            self.xt_data = xtdata

            # 连接交易
            session_id = int(datetime.now().timestamp() % 100000)
            xt_trader = xttrader.XtQuantTrader(path, session_id)
            xt_trader.start()
            connect_result = xt_trader.connect()

            if connect_result == 0:
                self.xt_trader = xt_trader
                # 订阅账户
                accounts = xt_trader.query_stock_asset(0)
                if accounts:
                    self.account_id = accounts.account_id
                self.connected = True
                print(f"[QMT] 连接成功，账户: {self.account_id}")
                return True
            else:
                print(f"[QMT] 连接失败，错误码: {connect_result}")
                return False

        except ImportError:
            print("[QMT] 未安装 xtquant 库，请先安装：pip install xtquant")
            print("[QMT] 如果 pip 安装失败，请从 QMT 安装目录的 bin.x64 下找到 xtquant 包")
            return False
        except Exception as e:
            print(f"[QMT] 连接异常: {e}")
            return False

    def disconnect(self) -> bool:
        """断开连接"""
        try:
            if self.xt_trader:
                self.xt_trader.stop()
                self.xt_trader = None
            self.connected = False
            return True
        except Exception as e:
            print(f"[QMT] 断开异常: {e}")
            return False

    def get_account(self) -> AccountInfo:
        """获取账户信息"""
        if not self.xt_trader:
            return AccountInfo(broker_name='QMT(未连接)')

        try:
            asset = self.xt_trader.query_stock_asset(self.account_id)
            if not asset:
                return AccountInfo(broker_name='QMT')

            return AccountInfo(
                broker_name='QMT',
                account_id=self.account_id,
                total_assets=asset.total_asset,
                available_cash=asset.cash,
                frozen_cash=asset.frozen_cash,
                market_value=asset.market_value,
                total_profit=asset.total_profit or 0,
                total_profit_rate=0,
                daily_profit=0,
                update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )
        except Exception as e:
            print(f"[QMT] 获取账户失败: {e}")
            return AccountInfo(broker_name='QMT(异常)')

    def get_positions(self) -> Dict[str, PositionInfo]:
        """获取持仓"""
        positions = {}
        if not self.xt_trader:
            return positions

        try:
            pos_list = self.xt_trader.query_stock_positions(self.account_id)
            for pos in pos_list:
                ts_code = pos.stock_code
                # QMT 返回的代码带市场后缀，如 '600519.SH'
                if '.' in ts_code:
                    code = ts_code.split('.')[0]
                else:
                    code = ts_code

                positions[code] = PositionInfo(
                    ts_code=code,
                    name=pos.stock_name or '',
                    quantity=pos.volume,
                    available_quantity=pos.can_use_volume,
                    cost_price=pos.avg_price,
                    current_price=pos.open_price or pos.avg_price,
                    market_value=pos.market_value,
                    profit=pos.float_profit or 0,
                    profit_rate=pos.float_profit / (pos.avg_price * pos.volume)
                    if pos.avg_price > 0 and pos.volume > 0 else 0
                )

        except Exception as e:
            print(f"[QMT] 获取持仓失败: {e}")

        return positions

    def submit_order(self, request: OrderRequest) -> OrderResult:
        """提交订单"""
        if not self.xt_trader:
            return OrderResult(
                order_id='',
                ts_code=request.ts_code,
                side=request.side,
                price=request.price,
                quantity=request.quantity,
                status=OrderStatus.REJECTED,
                error_message='QMT未连接',
                create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )

        try:
            # QMT 需要带市场后缀的代码
            if not request.ts_code.endswith(('.SH', '.SZ')):
                stock_code = request.ts_code + ('.SH' if request.ts_code.startswith('6') else '.SZ')
            else:
                stock_code = request.ts_code

            # 下单类型: 0-限价 1-市价
            price_type = 1 if request.order_type == OrderType.MARKET else 0

            # 买卖方向: 23-买入 24-卖出
            order_type = 23 if request.side == OrderSide.BUY else 24

            # 市价单价格填0
            price = 0 if request.order_type == OrderType.MARKET else request.price

            # 下单
            order_id = self.xt_trader.order_stock(
                self.account_id,
                stock_code,
                order_type,
                request.quantity,
                price_type,
                price,
                request.reason or '量化策略'
            )

            result = OrderResult(
                order_id=str(order_id),
                ts_code=request.ts_code,
                side=request.side,
                price=price,
                quantity=request.quantity,
                status=OrderStatus.PENDING,
                create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                reason=request.reason
            )

            if order_id <= 0:
                result.status = OrderStatus.REJECTED
                result.error_message = f'下单失败，错误码: {order_id}'
            else:
                result.status = OrderStatus.PENDING

            return result

        except Exception as e:
            return OrderResult(
                order_id='',
                ts_code=request.ts_code,
                side=request.side,
                price=request.price,
                quantity=request.quantity,
                status=OrderStatus.REJECTED,
                error_message=str(e),
                create_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            )

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单"""
        if not self.xt_trader:
            return False
        try:
            result = self.xt_trader.cancel_order_stock(self.account_id, int(order_id))
            return result == 0
        except Exception:
            return False

    def get_orders(self, status: OrderStatus = None, limit: int = 50) -> List[OrderResult]:
        """获取订单列表"""
        orders = []
        if not self.xt_trader:
            return orders

        try:
            order_list = self.xt_trader.query_stock_orders(self.account_id)
            for o in order_list[:limit]:
                side = OrderSide.BUY if o.order_type == 23 else OrderSide.SELL

                # QMT 状态码映射
                status_map = {
                    48: OrderStatus.PENDING,
                    49: OrderStatus.PARTIAL,
                    50: OrderStatus.FILLED,
                    51: OrderStatus.PARTIAL,
                    52: OrderStatus.FILLED,
                    53: OrderStatus.CANCELLED,
                    54: OrderStatus.REJECTED,
                    55: OrderStatus.EXPIRED,
                    56: OrderStatus.FILLED,
                    57: OrderStatus.FILLED,
                }
                order_status = status_map.get(o.order_status, OrderStatus.PENDING)

                # 过滤状态
                if status and order_status != status:
                    continue

                code = o.stock_code.split('.')[0] if '.' in o.stock_code else o.stock_code

                orders.append(OrderResult(
                    order_id=str(o.order_id),
                    ts_code=code,
                    side=side,
                    price=o.price,
                    quantity=o.order_volume,
                    filled_quantity=o.traded_volume,
                    filled_price=o.traded_price if o.traded_volume > 0 else 0,
                    status=order_status,
                    amount=o.traded_amount if hasattr(o, 'traded_amount') else o.traded_volume * o.traded_price,
                    create_time=o.order_time,
                    update_time=o.order_time,
                ))

        except Exception as e:
            print(f"[QMT] 获取订单失败: {e}")

        return orders

    def get_order(self, order_id: str) -> Optional[OrderResult]:
        """查询单个订单"""
        orders = self.get_orders()
        for o in orders:
            if o.order_id == order_id:
                return o
        return None

    def get_realtime_quote(self, ts_code: str) -> dict:
        """获取实时行情（QMT xtdata）"""
        if not self.xt_data:
            return {}

        try:
            stock_code = ts_code + ('.SH' if ts_code.startswith('6') else '.SZ')
            data = self.xt_data.get_full_tick([stock_code])
            if data and stock_code in data:
                tick = data[stock_code]
                return {
                    'ts_code': ts_code,
                    'last_price': tick.get('lastPrice', 0),
                    'open': tick.get('open', 0),
                    'high': tick.get('high', 0),
                    'low': tick.get('low', 0),
                    'volume': tick.get('volume', 0),
                    'amount': tick.get('amount', 0),
                    'bid1': tick.get('bidPrice', [0])[0] if tick.get('bidPrice') else 0,
                    'ask1': tick.get('askPrice', [0])[0] if tick.get('askPrice') else 0,
                }
        except Exception as e:
            print(f"[QMT] 获取行情失败 {ts_code}: {e}")

        return {}

"""
盯盘模块 — 自选股状态 + 异动拉升 + 主力资金流 + 涨跌停 + 板块热点

组件:
    MarketWatcherEngine — 单例编排引擎，3秒轮询分发
    PoolStatusTracker    — 自选股状态快照，按涨跌幅排序
    SurgeWatcher          — 异动拉升/跳水检测（60秒窗口 ±1.5%）
    FlowWatcher           — 主力资金流监测（内外盘增量 1分钟 ±5000万）
    LimitUpDownWatcher   — 全市场涨跌停扫描（分批50只/批，30秒/轮）
    SectorHeatmap         — 板块热点（TDX板块涨幅排名，30秒刷新）
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import os
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DATA_SOURCE_CONFIG
from utils.logger import get_logger
from broker.monitor import TDXQuotesPoller, QuoteSnapshot, SSEManager

logger = get_logger('live_trading', 'live_trading.log')


# ======================================================================
# 1. PoolStatusTracker — 自选股状态
# ======================================================================


class PoolStatusTracker:
    """自选股状态追踪器 — 每3秒产出快照，按涨跌幅降序排列。

    不单独记日志（高频数据），仅通过 SSE 推送到前端。
    """

    def process(self, curr: Dict[str, QuoteSnapshot]) -> List[dict]:
        """输入本轮 poll 快照，输出 SSE 事件列表。

        Args:
            curr: {code: QuoteSnapshot}

        Returns:
            [{"type": "pool_snapshot", "data": [...], "timestamp": "..."}]
        """
        stocks = []
        for code, snap in curr.items():
            stocks.append({
                "code": code,
                "name": snap.name,
                "price": snap.price,
                "change_pct": snap.change_pct,
                "volume": snap.volume,
                "amount": snap.amount,
                "bid1": snap.bid1,
                "ask1": snap.ask1,
                "high": snap.high,
                "low": snap.low,
            })
        stocks.sort(key=lambda x: x["change_pct"], reverse=True)
        return [{
            "type": "pool_snapshot",
            "data": stocks,
            "timestamp": datetime.now().isoformat(),
        }]


# ======================================================================
# 2. SurgeWatcher — 异动拉升/跳水
# ======================================================================


class SurgeWatcher:
    """异动拉升/跳水检测。

    维护 60 秒滑动窗口的价格历史。每次 poll 后比较最新价与窗口起始价，
    涨跌幅超过 ±1.5% 时触发告警。触发后清空窗口避免重复。
    """

    def __init__(self, surge_up_pct: float = 1.5, surge_down_pct: float = -1.5):
        """
        Args:
            surge_up_pct: 拉升阈值（%），正数
            surge_down_pct: 跳水阈值（%），负数
        """
        self.surge_up_pct = surge_up_pct
        self.surge_down_pct = surge_down_pct
        self._price_history: Dict[str, list] = {}

    def process(self, curr: Dict[str, QuoteSnapshot]) -> List[dict]:
        """处理本轮快照，返回异动告警列表。

        Args:
            curr: {code: QuoteSnapshot}

        Returns:
            [{"type": "surge_up", "code": ..., "name": ..., "change_pct": ..., ...}, ...]
        """
        alerts = []
        now = time.time()

        for code, snap in curr.items():
            if code not in self._price_history:
                self._price_history[code] = []
            history = self._price_history[code]
            history.append((now, snap.price))
            history[:] = [(t, p) for t, p in history if now - t <= 60]

            if len(history) < 2:
                continue

            first_price = history[0][1]
            if first_price <= 0:
                continue

            pct = (snap.price - first_price) / first_price * 100

            if pct >= self.surge_up_pct:
                alerts.append({
                    "type": "surge_up",
                    "code": code,
                    "name": snap.name,
                    "price": snap.price,
                    "change_pct": round(pct, 2),
                    "from_price": first_price,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("异动拉升", extra={"data": {
                    "code": code, "name": snap.name,
                    "change_pct": round(pct, 2), "from": first_price, "to": snap.price,
                }})
                self._price_history[code] = []

            elif pct <= self.surge_down_pct:
                alerts.append({
                    "type": "surge_down",
                    "code": code,
                    "name": snap.name,
                    "price": snap.price,
                    "change_pct": round(pct, 2),
                    "from_price": first_price,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("异动跳水", extra={"data": {
                    "code": code, "name": snap.name,
                    "change_pct": round(pct, 2), "from": first_price, "to": snap.price,
                }})
                self._price_history[code] = []

        return alerts


# ======================================================================
# 3. FlowWatcher — 主力资金流
# ======================================================================


class FlowWatcher:
    """主力资金流监测。

    利用 TDX 内外盘数据，计算每轮 poll 的主动买/卖增量。
    1 分钟内净流入/流出超过 5000 万时触发告警。
    """

    def __init__(
        self,
        inflow_1m: float = 50_000_000,
        outflow_1m: float = -50_000_000,
    ):
        """
        Args:
            inflow_1m: 1分钟净流入阈值（元），正数
            outflow_1m: 1分钟净流出阈值（元），负数
        """
        self.inflow_1m = inflow_1m
        self.outflow_1m = outflow_1m
        self._prev_snapshots: Dict[str, QuoteSnapshot] = {}
        self._flow_windows: Dict[str, list] = {}

    def process(self, curr: Dict[str, QuoteSnapshot]) -> List[dict]:
        """处理本轮快照，返回资金流告警列表。

        Args:
            curr: {code: QuoteSnapshot}

        Returns:
            [{"type": "big_inflow", "code": ..., "net_flow": ..., ...}, ...]
        """
        alerts = []
        now = time.time()

        for code, snap in curr.items():
            prev = self._prev_snapshots.get(code)
            if prev is None:
                self._prev_snapshots[code] = snap
                continue

            delta_buy = snap.active_buy - (prev.active_buy or 0)
            delta_sell = snap.active_sell - (prev.active_sell or 0)
            net_flow = delta_buy - delta_sell

            if code not in self._flow_windows:
                self._flow_windows[code] = []
            window = self._flow_windows[code]
            window.append((now, net_flow))
            window[:] = [(t, f) for t, f in window if now - t <= 60]

            total_1m = sum(f for _, f in window)
            total_in = sum(f for _, f in window if f > 0)
            total_out = abs(sum(f for _, f in window if f < 0))

            if total_1m >= self.inflow_1m:
                alerts.append({
                    "type": "big_inflow",
                    "code": code,
                    "name": snap.name,
                    "net_flow": total_1m,
                    "inflow": total_in,
                    "outflow": total_out,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("主力资金大幅流入", extra={"data": {
                    "code": code, "name": snap.name,
                    "net_flow_1m": total_1m, "inflow": total_in, "outflow": total_out,
                }})
                self._flow_windows[code] = []

            elif total_1m <= self.outflow_1m:
                alerts.append({
                    "type": "big_outflow",
                    "code": code,
                    "name": snap.name,
                    "net_flow": total_1m,
                    "inflow": total_in,
                    "outflow": total_out,
                    "timestamp": datetime.now().isoformat(),
                })
                logger.info("主力资金大幅流出", extra={"data": {
                    "code": code, "name": snap.name,
                    "net_flow_1m": abs(total_1m), "inflow": total_in, "outflow": total_out,
                }})
                self._flow_windows[code] = []

            self._prev_snapshots[code] = snap

        return alerts


# ======================================================================
# 4. LimitUpDownWatcher — 全市场涨跌停扫描
# ======================================================================


class LimitUpDownWatcher:
    """涨跌停监控（全市场扫描）。

    每30秒全市场分批扫描涨跌停股票。分批50只/批，间隔0.3秒防封。
    过滤涨跌幅 >= 9.5%的股票，计算封单强度分级。
    """

    def __init__(self, scan_interval: float = 30.0):
        """
        Args:
            scan_interval: 扫描间隔（秒），默认30秒
        """
        self.scan_interval = scan_interval
        self._last_scan: float = 0
        self._cached_stock_list: List[str] = []
        self._cached_stock_names: Dict[str, str] = {}  # code → name

    def _get_full_stock_list(self) -> List[str]:
        """获取全市场A股代码列表（缓存，首次拉取后复用）。

        Returns:
            纯数字代码列表，如 ['600519', '000001', ...]
        """
        if self._cached_stock_list:
            return self._cached_stock_list
        try:
            from pytdx.hq import TdxHq_API
            api = TdxHq_API()
            servers = DATA_SOURCE_CONFIG.get("tdx", {}).get("servers", [])
            for ip, port in servers:
                try:
                    if api.connect(ip, port):
                        codes = []
                        for start in range(0, 5000, 1000):
                            stocks = api.get_security_list(0, start)
                            if not stocks:
                                break
                            for s in stocks:
                                code = s["code"]
                                if code.startswith(("0", "3")):
                                    codes.append(code)
                                    name = s.get("name", "") or s.get("stock_name", "")
                                    if name and code not in self._cached_stock_names:
                                        self._cached_stock_names[code] = name
                        for start in range(0, 5000, 1000):
                            stocks = api.get_security_list(1, start)
                            if not stocks:
                                break
                            for s in stocks:
                                code = s["code"]
                                if code.startswith("6"):
                                    codes.append(code)
                                    name = s.get("name", "") or s.get("stock_name", "")
                                    if name and code not in self._cached_stock_names:
                                        self._cached_stock_names[code] = name
                        api.disconnect()
                        self._cached_stock_list = codes
                        logger.info(f"全市场股票列表加载: {len(codes)}只, {len(self._cached_stock_names)}个名称")
                        return codes
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"全市场股票列表获取失败: {e}")
        return []

    def process(self) -> List[dict]:
        """每30秒扫描一次，返回涨跌停告警列表。

        Returns:
            [{"type": "limit_up", "code": ..., "seal_strength": ..., ...}, ...]
        """
        now = time.time()
        if now - self._last_scan < self.scan_interval:
            return []
        self._last_scan = now

        codes = self._get_full_stock_list()
        if not codes:
            return []

        logger.debug("涨跌停扫描开始", extra={"data": {"total_stocks": len(codes)}})
        t0 = time.time()
        alerts = self._scan_batches(codes)
        elapsed = time.time() - t0
        up = sum(1 for a in alerts if a["type"] == "limit_up")
        down = sum(1 for a in alerts if a["type"] == "limit_down")
        logger.debug("涨跌停扫描完成", extra={"data": {
            "limit_up": up, "limit_down": down, "elapsed_s": round(elapsed, 1),
        }})
        return alerts

    def _scan_batches(self, codes: List[str]) -> List[dict]:
        """分批扫描，每批50只，间隔0.3秒。

        Args:
            codes: 全市场股票代码列表

        Returns:
            涨跌停告警列表
        """
        alerts: List[dict] = []
        batch_size = 50
        try:
            from pytdx.hq import TdxHq_API
            api = TdxHq_API()
            servers = DATA_SOURCE_CONFIG.get("tdx", {}).get("servers", [])
            connected = False
            for ip, port in servers:
                try:
                    if api.connect(ip, port):
                        connected = True
                        logger.debug(f"涨跌停扫描连接: {ip}:{port}")
                        break
                except Exception:
                    continue
            if not connected:
                logger.warning("涨跌停扫描: 所有服务器连接失败")
                return []

            for i in range(0, len(codes), batch_size):
                batch = codes[i:i + batch_size]
                try:
                    queries = [(1 if c.startswith("6") else 0, c) for c in batch]
                    quotes = api.get_security_quotes(queries)
                    if not quotes:
                        continue
                    for q in quotes:
                        if q is None:
                            continue
                        price = float(q.get("price", 0) or 0)
                        last_close = float(q.get("last_close", 0) or 0)
                        if price <= 0 or last_close <= 0:
                            continue
                        pct = (price - last_close) / last_close * 100
                        if abs(pct) < 9.5:
                            continue

                        code = str(q.get("code", "")).strip()
                        # TDX批量查询不返回名称，从缓存映射补充
                        name = str(q.get("name", "")).strip()
                        if not name and code in self._cached_stock_names:
                            name = self._cached_stock_names[code]
                        bid_vol = float(q.get("bid_vol1", 0) or 0)
                        ask_vol = float(q.get("ask_vol1", 0) or 0)
                        seal_vol = bid_vol if pct > 0 else ask_vol
                        seal_amount = seal_vol * 100 * price
                        if seal_amount >= 200_000_000:
                            strength = "强"
                        elif seal_amount >= 50_000_000:
                            strength = "中"
                        else:
                            strength = "弱"

                        alert = {
                            "type": "limit_up" if pct > 0 else "limit_down",
                            "code": code,
                            "name": name,
                            "change_pct": round(pct, 2),
                            "seal_amount": round(seal_amount, 2),
                            "seal_strength": strength,
                            "price": price,
                            "timestamp": datetime.now().isoformat(),
                        }
                        alerts.append(alert)
                        logger.info("涨跌停检测", extra={"data": {
                            "code": code, "change_pct": round(pct, 2),
                            "seal_strength": strength, "seal_amount": seal_amount,
                        }})
                except Exception as e:
                    logger.debug(f"涨跌停批次扫描异常: {e}")
                time.sleep(0.3)

            api.disconnect()
        except Exception as e:
            logger.error(f"涨跌停扫描失败: {e}", exc_info=True)
        return alerts


# ======================================================================
# 5. SectorHeatmap — 板块热点
# ======================================================================


class SectorHeatmap:
    """板块热点 — 每30秒获取TDX板块数据，计算资金流向和涨跌幅。

    数据来源:
    1. TDX block_gn.dat / block.dat → 板块成员股代码
    2. 当前 poll 快照 → 实时价格和内外盘数据
    3. 交叉计算: 板块均涨幅、主力净流入、领涨股
    """

    def __init__(self, refresh_interval: float = 30.0):
        """
        Args:
            refresh_interval: 刷新间隔（秒），默认30秒
        """
        self.refresh_interval = refresh_interval
        self._last_refresh: float = 0
        self._cached_data: dict = {}
        # 缓存板块→成员股代码映射（每天刷新一次）
        self._sector_stocks: Dict[str, List[str]] = {}
        self._sector_stocks_date: str = ""

    def process(self, curr_quotes: Dict[str, "QuoteSnapshot"] = None) -> dict:
        """获取板块热点数据（30秒内返回缓存）。

        Args:
            curr_quotes: 当前轮询快照 {code: QuoteSnapshot}，用于计算板块资金流

        Returns:
            {"concept": [...], "industry": [...], "timestamp": "..."}
            每个板块包含: name, change_pct, net_flow, leading_stock, stock_count
        """
        now = time.time()
        if now - self._last_refresh < self.refresh_interval:
            # 用最新快照更新缓存中的资金流数据
            if curr_quotes and self._cached_data:
                self._cached_data = self._enrich_with_quotes(self._cached_data, curr_quotes)
            return self._cached_data
        self._last_refresh = now

        result: dict = {"concept": [], "industry": [], "timestamp": datetime.now().isoformat()}
        try:
            from pytdx.hq import TdxHq_API
            api = TdxHq_API()
            servers = DATA_SOURCE_CONFIG.get("tdx", {}).get("servers", [])
            for ip, port in servers:
                try:
                    if api.connect(ip, port):
                        break
                except Exception:
                    continue
            else:
                logger.warning("板块热点: TDX连接失败")
                return self._cached_data

            # 检查是否需要刷新板块→成员映射（每天一次）
            today = datetime.now().strftime("%Y%m%d")
            need_rebuild = (today != self._sector_stocks_date) or (not self._sector_stocks)

            for block_file, key in [("block_gn.dat", "concept"), ("block.dat", "industry")]:
                try:
                    blocks = api.get_and_parse_block_info(block_file)
                    seen_blocks = set()
                    sector_list = []
                    for b in blocks:
                        block_name = b.get("blockname", "")
                        if not block_name or len(block_name) < 2 or '\x00' in block_name:
                            continue
                        valid = True
                        for char in block_name:
                            cp = ord(char)
                            if not ((0x20 <= cp <= 0x7E) or (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF)):
                                valid = False
                                break
                        if not valid:
                            continue
                        if block_name not in seen_blocks:
                            seen_blocks.add(block_name)
                            stock_code = b.get("code", "")
                            sector_list.append({
                                "code": stock_code,
                                "name": block_name,
                                "type": key,
                                "change_pct": 0,
                                "net_flow": 0,
                                "leading_stock": "",
                                "leading_pct": 0,
                                "stock_count": 0,
                            })
                            # 缓存板块→成员股映射
                            if need_rebuild:
                                if block_name not in self._sector_stocks:
                                    self._sector_stocks[block_name] = []
                                if stock_code and stock_code not in self._sector_stocks[block_name]:
                                    self._sector_stocks[block_name].append(stock_code)
                        if len(sector_list) >= 50:
                            break
                    result[key] = sector_list
                except Exception as e:
                    logger.debug(f"板块数据获取失败 {block_file}: {e}")

            if need_rebuild:
                self._sector_stocks_date = today

            api.disconnect()

            # 用当前快照丰富板块数据
            if curr_quotes:
                result = self._enrich_with_quotes(result, curr_quotes)

            logger.info("板块热点刷新", extra={"data": {
                "concept_count": len(result["concept"]),
                "industry_count": len(result["industry"]),
            }})
        except Exception as e:
            logger.warning(f"板块热点获取失败, 使用缓存: {e}")
            return self._cached_data

        self._cached_data = result
        return result

    def _enrich_with_quotes(self, sector_data: dict, curr_quotes: Dict[str, "QuoteSnapshot"]) -> dict:
        """用实时行情快照计算板块级别的涨跌幅和资金流向。

        对每个板块，汇总属于该板块且在当前快照中的股票的:
        - 平均涨跌幅
        - 净主动买卖 (active_buy - active_sell)
        - 领涨股
        """
        for key in ("concept", "industry"):
            for sector in sector_data.get(key, []):
                sector_name = sector.get("name", "")
                member_codes = self._sector_stocks.get(sector_name, [])
                if not member_codes:
                    continue

                total_change = 0.0
                total_net_flow = 0.0
                matched = 0
                leading_code = ""
                leading_pct = -999

                for code in member_codes:
                    snap = curr_quotes.get(code)
                    if snap is None:
                        continue
                    matched += 1
                    total_change += snap.change_pct
                    net = (snap.active_buy or 0) - (snap.active_sell or 0)
                    total_net_flow += net
                    if snap.change_pct > leading_pct:
                        leading_pct = snap.change_pct
                        leading_code = code

                sector["change_pct"] = round(total_change / matched, 2) if matched > 0 else 0
                sector["net_flow"] = round(total_net_flow, 2)
                sector["leading_stock"] = leading_code
                sector["leading_pct"] = leading_pct if leading_code else 0
                sector["stock_count"] = matched

        return sector_data


# ======================================================================
# 6. MarketWatcherEngine — 编排引擎（单例）
# ======================================================================


class MarketWatcherEngine:
    """盯盘引擎（单例）— 编排5个子模块，管理生命周期。

    生命周期:
    1. start(stock_pool) → 加载名称 → 启动后台轮询线程
    2. _poll_loop() → 每3秒轮询 → 分发给5个子模块 → SSE推送
    3. stop() → 停止线程 → 清理资源

    线程安全:
    - 轮询在 daemon 线程中执行
    - SSE 推送在 asyncio 事件循环中执行
    - 单例使用 threading.Lock 保护
    """

    _instance: Optional["MarketWatcherEngine"] = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "MarketWatcherEngine":
        """获取单例实例（线程安全）。"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self):
        if hasattr(self, "_poller"):
            return
        self._poller = TDXQuotesPoller()
        self._sse = SSEManager()
        self._pool_tracker = PoolStatusTracker()
        self._surge_watcher = SurgeWatcher()
        self._flow_watcher = FlowWatcher()
        self._limit_watcher = LimitUpDownWatcher()
        self._sector_heatmap = SectorHeatmap()

        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._stock_pool: List[str] = []
        self._stock_names: Dict[str, str] = {}
        self._interval: float = 3.0
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._poll_count: int = 0

        logger.info("MarketWatcher 初始化完成")

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running.is_set()

    # ------------------------------------------------------------------
    # 事件循环
    # ------------------------------------------------------------------

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """设置 asyncio 事件循环（由 uvicorn startup 回调调用）。"""
        self._event_loop = loop
        logger.info("MarketWatcher 事件循环已绑定")

    # ------------------------------------------------------------------
    # 股票名称
    # ------------------------------------------------------------------

    def _load_stock_names(self) -> None:
        """从数据库加载股票 code → name 映射。"""
        try:
            from data.database import SQLiteManager
            db = SQLiteManager()
            rows = db._conn.execute(
                "SELECT ts_code, name FROM stock_info"
            ).fetchall()
            for row in rows:
                code = row["ts_code"].replace(".SH", "").replace(".SZ", "")
                if row["name"]:
                    self._stock_names[code] = row["name"]
            db.close()
        except Exception as e:
            logger.error(f"股票名称加载失败: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self, stock_pool: List[str]) -> dict:
        """启动盯盘。

        Args:
            stock_pool: 股票代码列表 ['600519', '000001', ...]

        Returns:
            {"status": "started", "stock_count": N} 或 {"status": "already_running"}
        """
        with self._lock:
            if self._running.is_set():
                return {"status": "already_running", "stock_count": len(self._stock_pool)}

            self._stock_pool = list(stock_pool)
            if not self._stock_pool:
                return {"status": "error", "message": "股票池为空"}

            self._running.set()
            self._load_stock_names()

            self._thread = threading.Thread(
                target=self._poll_loop,
                daemon=True,
                name="market-watcher",
            )
            self._thread.start()
            logger.info("MarketWatcher 已启动", extra={
                "data": {"stock_count": len(stock_pool)},
            })
            return {"status": "started", "stock_count": len(stock_pool)}

    def stop(self) -> dict:
        """停止盯盘。"""
        with self._lock:
            if not self._running.is_set():
                return {"status": "not_running"}
            self._running.clear()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

        logger.info("MarketWatcher 已停止")
        return {"status": "stopped"}

    # ------------------------------------------------------------------
    # 轮询循环
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        """3秒轮询 → 快照分发给各子模块 → SSE推送。"""
        logger.info("MarketWatcher 轮询循环启动")

        while self._running.is_set():
            try:
                if not TDXQuotesPoller.is_trading_time():
                    time.sleep(30)
                    continue

                curr = self._poller.poll(self._stock_pool)
            except Exception as e:
                logger.error(f"轮询失败: {e}", exc_info=True)
                time.sleep(10)
                continue

            # 注入数据库股票名称
            if self._stock_names and curr:
                curr = {
                    code: dataclasses.replace(
                        snap, name=self._stock_names.get(code, snap.name)
                    )
                    for code, snap in curr.items()
                }

            if not curr:
                time.sleep(self._interval)
                continue

            # 分发给各子模块（每个子模块独立 try/except，防止单点崩溃）
            try:
                pool_data = self._pool_tracker.process(curr)
            except Exception as e:
                logger.error(f"自选股快照异常: {e}", exc_info=True)
                pool_data = []
            try:
                surge_alerts = self._surge_watcher.process(curr)
            except Exception as e:
                logger.error(f"异动检测异常: {e}", exc_info=True)
                surge_alerts = []
            try:
                flow_alerts = self._flow_watcher.process(curr)
            except Exception as e:
                logger.error(f"资金流检测异常: {e}", exc_info=True)
                flow_alerts = []
            try:
                limit_alerts = self._limit_watcher.process()
            except Exception as e:
                logger.error(f"涨跌停扫描异常: {e}", exc_info=True)
                limit_alerts = []
            # 涨跌停走独立TDX批量查询，不走curr轮询，需单独注入数据库名称
            if self._stock_names and limit_alerts:
                for a in limit_alerts:
                    code = a.get("code", "")
                    if code and (not a.get("name") or a["name"] == code):
                        a["name"] = self._stock_names.get(code, a.get("name", code))
            try:
                sector_data = self._sector_heatmap.process(curr)
            except Exception as e:
                logger.error(f"板块热点异常: {e}", exc_info=True)
                sector_data = {}

            # SSE 推送
            # pool_data[0] 已包含 {"type": "pool_snapshot", "data": [...], "timestamp": "..."}
            # 前端期望 {"type": "pool_snapshot", "data": [...]}
            if pool_data:
                snapshot = pool_data[0]
                self._push_sse("pool_snapshot", snapshot.get("data", []))
            for alert in surge_alerts + flow_alerts + limit_alerts:
                self._push_sse("alert", alert)
            if sector_data:
                self._push_sse("sector_heatmap", sector_data)

            # 统计心跳
            stats = {
                "limit_up": sum(1 for a in limit_alerts if a.get("type") == "limit_up"),
                "limit_down": sum(1 for a in limit_alerts if a.get("type") == "limit_down"),
                "surge": len(surge_alerts),
                "flow": len(flow_alerts),
            }
            self._push_sse("stats", stats)

            self._poll_count += 1
            if self._poll_count % 10 == 0:
                logger.debug(f"盯盘心跳: 第{self._poll_count}轮, 快照={len(curr)}只")

            time.sleep(self._interval)

        logger.info("MarketWatcher 轮询循环退出")

    def _push_sse(self, event_type: str, data: Any) -> None:
        """跨线程 SSE 推送（asyncio 安全）。

        Args:
            event_type: SSE 事件类型
            data: 推送数据（dict 或 JSON 字符串）
        """
        loop = self._event_loop
        if loop is None or not loop.is_running():
            return
        try:
            # 包裹 type 字段，前端通过 msg.type 分发
            payload = json.dumps({"type": event_type, "data": data}, ensure_ascii=False, default=str)
            asyncio.run_coroutine_threadsafe(self._sse.push(payload), loop)
        except Exception:
            logger.debug(f"SSE推送失败: {event_type}")

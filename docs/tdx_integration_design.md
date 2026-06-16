# 通达信 (TDX) 数据源集成设计方案

## 1. 现状分析

### 当前架构

```
live_server.py / server.py (回测)
        │
        ▼
    DataFetcher  ←── 直接依赖 akshare / 东方财富 HTTP
        │
        ▼
    SQLiteManager  ←── 存储日线/分钟线/基本面
```

**问题：**
- `DataFetcher` 硬编码 AKShare，无抽象层
- 实时行情爬网页，速度慢（秒级）
- 无 5 档盘口、分笔成交、历史分钟线
- AKShare 受反爬限制，不稳定

### TDX vs AKShare 对比

| 数据类型 | 通达信 (pytdx) | AKShare |
|----------|---------------|---------|
| 日K线 | ✅ 毫秒级 | ✅ 秒级 |
| 实时行情 | ✅ 5档盘口+内外盘 | ⚠️ 仅基础OHLCV |
| 分笔成交 | ✅ 逐笔明细 | ❌ |
| 历史分钟线 | ✅ 240根/天 | ⚠️ 仅当日 |
| 除权除息 | ✅ 送配派明细 | ⚠️ 需另外接口 |
| 股票列表 | ✅ | ✅ |
| 财务数据 | ⚠️ 基础字段 | ✅ 更丰富 |
| 资金流向 | ❌ | ✅ 东方财富 |
| 北向资金 | ❌ | ✅ |
| 稳定性 | ✅ 直连服务器 | ⚠️ 依赖反爬 |

---

## 2. 设计目标

1. **数据源可插拔** — 抽象接口，支持 TDX / AKShare / 其他数据源热切换
2. **自动降级** — TDX 优先，失败自动回退 AKShare → DB 缓存
3. **格式兼容** — 对外 `market_data` 格式不变，策略/回测零改动
4. **速度提升** — 实时行情从秒级降到毫秒级，批量数据支持并发
5. **新增数据** — 盘口深度、分笔成交、历史分钟线入库

---

## 3. 架构设计

### 3.1 分层架构

```
                    live_server.py / server.py
                           │
                           ▼
                    DataFetcher (门面)
                    ┌─────────┴─────────┐
                    ▼                   ▼
            TDXDataSource       AKShareDataSource
            (pytdx)             (akshare)
                    │                   │
                    └─────────┬─────────┘
                              ▼
                      SQLiteManager (存储层)
```

### 3.2 新增文件

```
data/
├── fetcher.py          ← 改造：门面层，委托给数据源
├── sources/            ← 新增目录
│   ├── __init__.py     ← 数据源注册表 + 工厂函数
│   ├── base.py         ← BaseDataSource 抽象基类
│   ├── tdx_source.py   ← TDXDataSource (pytdx)
│   └── akshare_source.py ← AKShareDataSource (现有逻辑迁移)
├── database.py         ← 不变
└── validator.py        ← 不变
```

---

## 4. 抽象基类设计

### `data/sources/base.py`

```python
from abc import ABC, abstractmethod
from typing import List, Dict, Optional
import pandas as pd


class BaseDataSource(ABC):
    """数据源抽象基类 — 所有数据源必须实现此接口"""

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源名称，如 'tdx'、'akshare'"""
        ...

    @abstractmethod
    def get_stock_list(self) -> pd.DataFrame:
        """获取A股股票列表
        Returns:
            DataFrame 列: ts_code, symbol, name, area, industry, market, list_date
        """
        ...

    @abstractmethod
    def get_daily_data(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取日K线数据
        Args:
            ts_code: 如 '600519.SH' 或 '000001.SZ'
            start_date: YYYYMMDD
            end_date: YYYYMMDD
        Returns:
            DataFrame 列: trade_date, open, high, low, close, vol, amount, pct_chg, turnover_rate
        """
        ...

    @abstractmethod
    def get_realtime_quotes(self, codes: List[str]) -> Dict[str, dict]:
        """获取实时行情
        Args:
            codes: 股票代码列表，如 ['600519', '000001']
        Returns:
            {code: {close, open, high, low, volume, amount, pct_chg, bid1-5, ask1-5, ...}}
        """
        ...

    def get_stock_info(self, symbol: str) -> dict:
        """获取股票基本信息 (可选覆盖)
        Returns: {name, industry, market_cap, pe, pb}
        """
        return {}

    def get_financial_data(self, symbol: str) -> dict:
        """获取财务数据 (可选覆盖)
        Returns: {roe, gross_margin, profit_growth, revenue_growth, accrual_ratio, ...}
        """
        return {}

    def get_minute_data(self, ts_code: str, period: str = '5',
                        start_time: str = None, end_time: str = None) -> pd.DataFrame:
        """获取分钟K线 (可选覆盖)"""
        return pd.DataFrame()

    def get_xdxr_data(self, ts_code: str) -> list:
        """获取除权除息数据 (可选覆盖)"""
        return []

    def get_transaction_data(self, ts_code: str, count: int = 10) -> list:
        """获取分笔成交 (可选覆盖)"""
        return []
```

---

## 5. TDX 数据源实现

### `data/sources/tdx_source.py`

```python
"""
通达信数据源 — 基于 pytdx
- 直连行情服务器，速度快
- 5档盘口、分笔成交、历史分钟线
- 连接池支持并发
"""

import pandas as pd
from datetime import datetime
from typing import List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from pytdx.hq import TdxHq_API
from .base import BaseDataSource

# TDX 服务器池（自动选择可用服务器）
TDX_SERVERS = [
    ('60.191.117.167', 7709),
    ('120.76.152.2', 7709),
    ('121.14.104.72', 7709),
    ('218.108.98.244', 7709),
]

# 市场代码映射
SH_CODES = {'6'}      # 60xxxx, 68xxxx
SZ_CODES = {'0', '3'} # 00xxxx, 30xxxx


class TDXDataSource(BaseDataSource):
    """通达信数据源"""

    name = 'tdx'

    def __init__(self, servers: list = None, max_workers: int = 5):
        self._servers = servers or TDX_SERVERS
        self._max_workers = max_workers
        self._api: TdxHq_API = None

    def _connect(self) -> TdxHq_API:
        """获取连接（自动选择可用服务器）"""
        if self._api is not None:
            return self._api
        api = TdxHq_API()
        for ip, port in self._servers:
            try:
                if api.connect(ip, port):
                    self._api = api
                    return api
            except Exception:
                continue
        raise ConnectionError('所有 TDX 服务器连接失败')

    def _disconnect(self):
        if self._api:
            try:
                self._api.disconnect()
            except Exception:
                pass
            self._api = None

    def _parse_code(self, ts_code: str) -> tuple:
        """解析 ts_code -> (market, code)
        '600519.SH' -> (1, '600519')
        '000001.SZ' -> (0, '000001')
        """
        code = ts_code.replace('.SH', '').replace('.SZ', '')
        market = 1 if code[0] in SH_CODES else 0
        return market, code

    # ============================================================
    # 必须实现的方法
    # ============================================================

    def get_stock_list(self) -> pd.DataFrame:
        """获取全市场股票列表"""
        api = self._connect()
        rows = []

        for market in [0, 1]:  # 0=深圳, 1=上海
            count = api.get_security_count(market)
            # 分批获取（每批 1000 只）
            for start in range(0, count, 1000):
                stocks = api.get_security_list(market, start)
                for s in stocks:
                    code = s['code']
                    market_code = 'SH' if market == 1 else 'SZ'
                    rows.append({
                        'ts_code': f"{code}.{market_code}",
                        'symbol': code,
                        'name': s['name'],
                        'market': market_code,
                    })

        return pd.DataFrame(rows)

    def get_daily_data(self, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取日K线（含除权因子）"""
        api = self._connect()
        market, code = self._parse_code(ts_code)

        # 获取 K 线
        bars = api.get_security_bars(9, market, code, 0, 800)

        if not bars:
            return pd.DataFrame()

        rows = []
        for b in bars:
            date_str = f"{b['year']}{b['month']:02d}{b['day']:02d}"
            if start_date <= date_str <= end_date:
                rows.append({
                    'trade_date': f"{b['year']}-{b['month']:02d}-{b['day']:02d}",
                    'open': b['open'],
                    'high': b['high'],
                    'low': b['low'],
                    'close': b['close'],
                    'vol': b['vol'],
                    'amount': b['amount'],
                })

        df = pd.DataFrame(rows)
        if not df.empty:
            df['pct_chg'] = df['close'].pct_change() * 100
            df['ts_code'] = ts_code
        return df

    def get_realtime_quotes(self, codes: List[str]) -> Dict[str, dict]:
        """获取实时行情（含5档盘口、内外盘）"""
        api = self._connect()
        result = {}

        for code in codes:
            market = 1 if code[0] in SH_CODES else 0
            try:
                quotes = api.get_security_quotes([(market, code)])
                if quotes:
                    q = quotes[0]
                    result[code] = {
                        'close': q['price'],
                        'open': q['open'],
                        'high': q['high'],
                        'low': q['low'],
                        'volume': q['vol'],
                        'amount': q['amount'],
                        'pct_chg': (q['price'] / q['last_close'] - 1) * 100 if q['last_close'] else 0,
                        # 5 档盘口
                        'bid1': q['bid1'], 'bid_vol1': q['bid_vol1'],
                        'bid2': q['bid2'], 'bid_vol2': q['bid_vol2'],
                        'bid3': q['bid3'], 'bid_vol3': q['bid_vol3'],
                        'bid4': q['bid4'], 'bid_vol4': q['bid_vol4'],
                        'bid5': q['bid5'], 'bid_vol5': q['bid_vol5'],
                        'ask1': q['ask1'], 'ask_vol1': q['ask_vol1'],
                        'ask2': q['ask2'], 'ask_vol2': q['ask_vol2'],
                        'ask3': q['ask3'], 'ask_vol3': q['ask_vol3'],
                        'ask4': q['ask4'], 'ask_vol4': q['ask_vol4'],
                        'ask5': q['ask5'], 'ask_vol5': q['ask_vol5'],
                        # 内外盘
                        'buy_vol': q.get('active1', 0),
                        'sell_vol': q.get('active2', 0),
                    }
            except Exception:
                continue

        return result

    # ============================================================
    # TDX 独有的优势方法
    # ============================================================

    def get_minute_data(self, ts_code: str, period: str = '5',
                        start_time: str = None, end_time: str = None) -> pd.DataFrame:
        """获取历史分钟K线（TDX 优势：240根/天）"""
        api = self._connect()
        market, code = self._parse_code(ts_code)

        # 5分钟线: type=0
        bars = api.get_history_minute_time_data(market, code, 20260616)

        if not bars:
            return pd.DataFrame()

        rows = []
        for i, b in enumerate(bars):
            rows.append({
                'time': f"{i//4:02d}:{(i%4)*15:02d}",
                'price': b['price'],
                'volume': b['vol'],
                'ts_code': ts_code,
            })

        df = pd.DataFrame(rows)
        if not df.empty:
            df['period'] = '5min'
        return df

    def get_xdxr_data(self, ts_code: str) -> list:
        """获取除权除息"""
        api = self._connect()
        market, code = self._parse_code(ts_code)
        return api.get_xdxr_info(market, code)

    def get_transaction_data(self, ts_code: str, count: int = 100) -> list:
        """获取分笔成交"""
        api = self._connect()
        market, code = self._parse_code(ts_code)
        return api.get_transaction_data(market, code, 0, count)

    def get_block_stocks(self, block_name: str) -> list:
        """获取板块成分股"""
        api = self._connect()
        # 获取板块文件列表
        for fname in ['block_gn.dat', 'block.dat']:
            blocks = api.get_and_parse_block_info(fname)
            for b in blocks:
                if block_name in b.get('blockname', ''):
                    return api.get_block_stocks(b['code'])
        return []

    def __del__(self):
        self._disconnect()
```

---

## 6. DataFetcher 改造

### 门面模式 + 自动降级

```python
# data/fetcher.py

from data.sources import get_data_source
from data.sources.base import BaseDataSource

class DataFetcher:
    """数据获取门面 — 支持多数据源自动降级"""

    def __init__(self, primary: str = 'tdx', fallback: str = 'akshare'):
        """
        Args:
            primary: 主数据源 ('tdx' / 'akshare')
            fallback: 降级数据源
        """
        self._primary: BaseDataSource = get_data_source(primary)
        self._fallback: BaseDataSource = get_data_source(fallback)

    def _fetch(self, method: str, *args, **kwargs):
        """通用获取：主源失败自动降级"""
        try:
            return getattr(self._primary, method)(*args, **kwargs)
        except Exception as e:
            logger.warning(f'{self._primary.name} {method} 失败, 降级到 {self._fallback.name}: {e}')
            return getattr(self._fallback, method)(*args, **kwargs)

    def get_stock_list(self):
        return self._fetch('get_stock_list')

    def get_daily_data(self, ts_code, start_date, end_date):
        return self._fetch('get_daily_data', ts_code, start_date, end_date)

    def get_realtime_quotes(self, codes):
        return self._fetch('get_realtime_quotes', codes)
    # ... 其他方法同理

    # build_market_data_by_date 等复杂方法保持不变
```

### 数据源注册表

```python
# data/sources/__init__.py

DATA_SOURCE_REGISTRY = {
    'tdx': {
        'class': 'data.sources.tdx_source.TDXDataSource',
        'name': '通达信',
        'description': 'pytdx 直连行情服务器，支持5档盘口/分钟线/分笔成交',
    },
    'akshare': {
        'class': 'data.sources.akshare_source.AKShareDataSource',
        'name': 'AKShare',
        'description': '免费开源金融数据接口，数据丰富',
    },
}


def get_data_source(name: str = 'tdx', **kwargs):
    """工厂函数：获取数据源实例"""
    if name not in DATA_SOURCE_REGISTRY:
        raise ValueError(f'未知数据源: {name}，可用: {list(DATA_SOURCE_REGISTRY.keys())}')
    entry = DATA_SOURCE_REGISTRY[name]
    module_path, class_name = entry['class'].rsplit('.', 1)
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)(**kwargs)
```

---

## 7. 配置

### `config/settings.py` 新增

```python
# 数据源配置
DATA_SOURCE = {
    'primary': 'tdx',        # 主数据源: tdx / akshare
    'fallback': 'akshare',   # 降级数据源
    'tdx': {
        'servers': [
            ('60.191.117.167', 7709),
            ('120.76.152.2', 7709),
            ('121.14.104.72', 7709),
        ],
        'max_workers': 5,     # 并发连接数
        'connect_timeout': 5, # 连接超时（秒）
    },
}
```

---

## 8. 数据库升级

### 8.1 现有表兼容性

| 现有表 | TDX 数据 | 是否兼容 |
|--------|----------|----------|
| `daily_bars` | 日K线 | ✅ 字段完全匹配，无需改动 |
| `minute_bars` | 5/15/30/60分钟线 | ⚠️ 微调：新增 `amount` 字段 |
| `fundamentals` | 财务数据 | ⚠️ 现有 9 个字段，TDX 有 37 个 |
| `stock_info` | 股票基本信息 | ✅ 无需改动 |
| `trade_calendar` | — | ✅ 无需改动 |
| `data_log` | — | ✅ 无需改动 |
| `account_snapshot` | — | ✅ 无需改动 |

### 8.2 修改现有表

#### `minute_bars` — 新增字段

```sql
-- 在现有 minute_bars 表基础上新增 amount 列
ALTER TABLE minute_bars ADD COLUMN amount REAL;
```

TDX 分钟线返回 `price` 和 `vol` 两个字段；增加 `amount = price * vol` 便于后续分析。

#### `fundamentals` — 新增字段

```sql
-- 扩展基本面表，新增 TDX 独有的股本/资产/现金流字段
ALTER TABLE fundamentals ADD COLUMN total_shares     REAL;  -- 总股本
ALTER TABLE fundamentals ADD COLUMN float_shares     REAL;  -- 流通股本
ALTER TABLE fundamentals ADD COLUMN total_assets     REAL;  -- 总资产
ALTER TABLE fundamentals ADD COLUMN net_assets_ps    REAL;  -- 每股净资产
ALTER TABLE fundamentals ADD COLUMN operating_revenue REAL; -- 主营收入
ALTER TABLE fundamentals ADD COLUMN operating_profit REAL;  -- 主营利润
ALTER TABLE fundamentals ADD COLUMN operating_cf     REAL;  -- 经营现金流
ALTER TABLE fundamentals ADD COLUMN shareholder_count REAL; -- 股东人数
```

### 8.3 新增表

#### ① `xdxr` 除权除息表 — 🔴 重要

完整的除权除息数据是精确复权计算的基础，直接关系回测收益率的准确性。

```sql
CREATE TABLE IF NOT EXISTS xdxr (
    ts_code       TEXT NOT NULL,
    ex_date       TEXT NOT NULL,      -- 除权日期 YYYYMMDD
    category      INTEGER,            -- 1=除权除息, 5=股本变化
    name          TEXT,               -- 事件名称
    fenhong       REAL,               -- 每股分红（元）
    songzhuangu   REAL,               -- 每股送转股
    peigu         REAL,               -- 每股配股
    peigujia      REAL,               -- 配股价
    suogu         REAL,               -- 缩股比例
    qianzongguben REAL,               -- 前总股本
    houzongguben  REAL,               -- 后总股本
    fenshu        REAL,               -- 分数
    xingquanjia   REAL,               -- 行权价
    PRIMARY KEY (ts_code, ex_date)
);
```

**数据量估算：** 全市场约 5 万只股票 × 平均 20 条 = ~100 万条，SQLite 轻松承载。

#### ② `block_info` 板块成分股表 — 🟡 辅助

```sql
CREATE TABLE IF NOT EXISTS block_info (
    block_code  TEXT NOT NULL,
    block_name  TEXT NOT NULL,
    block_type  TEXT NOT NULL,        -- 'gn'概念 / 'hy'行业 / 'zs'指数
    ts_code     TEXT NOT NULL,
    stock_name  TEXT,
    PRIMARY KEY (block_code, ts_code)
);

CREATE INDEX IF NOT EXISTS idx_block_type ON block_info(block_type);
CREATE INDEX IF NOT EXISTS idx_block_ts ON block_info(ts_code);
```

**数据量估算：** 概念 ~140 万条 + 行业 ~38 万条 + 指数 ~21 万条 = ~200 万条。

#### ③ `finance_detail` 扩展财务表 — 🟡 辅助

存放 TDX 37 个原始财务字段（与 `fundamentals` 表互补）：

```sql
CREATE TABLE IF NOT EXISTS finance_detail (
    ts_code           TEXT NOT NULL,
    report_date       TEXT NOT NULL,  -- 财报截止日期
    -- 股本结构
    total_shares      REAL,           -- 总股本
    float_shares      REAL,           -- 流通股本
    state_shares      REAL,           -- 国家股
    legal_person_shares REAL,         -- 法人股
    b_shares          REAL,           -- B股
    h_shares          REAL,           -- H股
    employee_shares   REAL,           -- 职工股
    -- 资产负债
    total_assets      REAL,           -- 总资产
    current_assets    REAL,           -- 流动资产
    fixed_assets      REAL,           -- 固定资产
    intangible_assets REAL,           -- 无形资产
    net_equity        REAL,           -- 净资产
    current_liabilities REAL,         -- 流动负债
    long_term_liabilities REAL,       -- 长期负债
    -- 盈利
    operating_revenue REAL,           -- 主营收入
    operating_profit  REAL,           -- 主营利润
    business_profit   REAL,           -- 营业利润
    net_profit_after_tax REAL,        -- 税后净利润
    retained_earnings REAL,           -- 未分配利润
    -- 现金流
    operating_cf      REAL,           -- 经营活动现金流
    total_cf          REAL,           -- 总现金流
    -- 其他
    capital_reserve   REAL,           -- 资本公积金
    shareholder_count REAL,           -- 股东人数
    net_assets_ps     REAL,           -- 每股净资产
    investment_income REAL,           -- 投资收益
    inventory         REAL,           -- 存货
    receivables       REAL,           -- 应收账款
    ipo_date          TEXT,           -- 上市日期
    PRIMARY KEY (ts_code, report_date)
);
```

**数据量估算：** 每只股票每年 1-4 条，全市场约 10-20 万条，极小。

### 8.4 不需要存的

| 数据类型 | 数据量 | 为什么不存 |
|----------|--------|-----------|
| **5档盘口** | ~7200 万条/天 | SQLite 扛不住，实时消费即可 |
| **分笔成交** | ~千万条/天 | 需要 ClickHouse/InfluxDB 等时序库 |
| **板块文件** | 源文件(.dat) | 直接读 pytdx 解析即可，无需存库 |

> 盘口和分笔数据应遵循"实时获取、实时消费"模式——就像现在 `live_server.py` 获取实时 OHLCV 一样，拿到就用，不落地。

### 8.5 SQLiteManager 新增方法

```python
# 除权除息
def upsert_xdxr(self, xdxr_list: list):
    """批量 upsert 除权除息数据"""

# 板块成分
def upsert_block_info(self, block_type: str, blocks: list):
    """批量 upsert 板块成分股"""

def get_block_stocks(self, block_name: str) -> list:
    """查询板块成分股"""

# 扩展财务
def upsert_finance_detail(self, rows: list):
    """批量 upsert 扩展财务数据"""

def get_finance_detail(self, ts_code: str, trade_date: str) -> dict:
    """获取截止某交易日的最新扩展财务数据"""
```

---

## 9. 实施计划总览

> 详细步骤见 [12.5 更新后的实施步骤](#125-更新后的实施步骤)。总计 **14 步，约 9.5 小时**。

---

## 10. 风险与注意事项

1. **TDX 服务器稳定性** — 免费服务器可能变慢/失效，需维护服务器列表
2. **pytdx 依赖** — 纯 Python 实现，无 C 扩展，安装简单
3. **数据格式差异** — TDX 复权方式与 AKShare 不同，需统一复权计算
4. **并发安全** — pytdx 连接非线程安全，每个线程需独立连接
5. **向后兼容** — `build_market_data_by_date` 等核心方法输出格式不变，策略零改动
6. **编码** — TDX 返回 GBK 编码的中文，需 `.encode('gbk').decode('utf-8')` 处理

---

## 11. 回测与实盘升级

TDX 的分钟线和 5 档盘口数据，可以直接提升回测精度和实盘执行质量——策略代码完全不动。

### 11.1 回测：分钟线执行模拟器

#### 现状问题

当前 `BacktestEngine` 执行交易是"拍脑袋"式的：

```python
# backtest/matcher.py 当前逻辑
fill_price = open_price * (1 + slippage_rate)  # 滑点固定 0.2%
# 只要 fill_price 在当日最高/最低范围内就算成交
```

0.2% 的滑点率是写死的，与实际市场情况无关。对于流动性差的股票，实际滑点可能远超 0.2%；对于茅台这种大盘股，真实滑点可能不到 0.02%。

#### 改造方案：`MinuteBarExecutor`

新增一个可选执行器，利用历史分钟线计算**真实日内成交价**：

```python
class MinuteBarExecutor:
    """基于历史分钟线的执行模拟器"""

    def __init__(self, minute_data: pd.DataFrame):
        """
        Args:
            minute_data: 当日 240 根分钟线 (price, vol)
        """
        self._bars = minute_data
        self._vwap = self._calc_vwap()

    def _calc_vwap(self) -> float:
        """计算当日 VWAP（成交量加权均价）"""
        total_vol = sum(b['vol'] for b in self._bars)
        if total_vol == 0:
            return self._bars[0]['price']
        return sum(b['price'] * b['vol'] for b in self._bars) / total_vol

    def estimate_fill_price(
        self,
        side: str,        # BUY / SELL
        quantity: int,    # 目标数量
        time_index: int = 30,  # 假设第 30 根分钟线触发(09:45)
    ) -> dict:
        """
        预估成交价格

        逻辑：
        1. 从触发时间点开始，逐分钟累加成交量
        2. 直到累计量 >= 目标量
        3. 按成交量加权计算平均成交价
        4. 返回：预估价格、滑点率、能否成交
        """
        accumulated_vol = 0
        accumulated_amount = 0.0

        for i in range(time_index, len(self._bars)):
            bar = self._bars[i]
            vol_needed = quantity * 100 - accumulated_vol  # 还需要的股数
            take_vol = min(vol_needed, bar['vol'])

            accumulated_vol += take_vol
            accumulated_amount += bar['price'] * take_vol

            if accumulated_vol >= quantity * 100:
                avg_price = accumulated_amount / accumulated_vol
                return {
                    'fill_price': avg_price,
                    'slippage': (avg_price / self._bars[time_index]['price'] - 1) * 100,
                    'filled': True,
                    'bars_consumed': i - time_index + 1,
                }

        # 当日成交量不够
        return {
            'fill_price': accumulated_amount / accumulated_vol if accumulated_vol > 0 else self._bars[-1]['price'],
            'slippage': None,
            'filled': False,
            'partial_quantity': accumulated_vol // 100,
        }
```

#### BacktestEngine 集成

```python
# backtest/engine.py 中新增参数
class BacktestConfig:
    ...
    use_minute_execution: bool = False  # 新增：启用分钟线执行

# execute_backtest() 中
if config.use_minute_execution:
    # 从 DB 加载当日分钟线
    minute_bars = db.get_minute_bars(ts_code, date, period=5)
    executor = MinuteBarExecutor(minute_bars)
    fill_result = executor.estimate_fill_price(side, quantity)
else:
    # 原有逻辑：open * (1 + slippage)
    fill_price = open_price * (1 + config.slippage_rate)
```

**影响范围：**
- 新增 `backtest/minute_executor.py`（约 80 行）
- `backtest/engine.py`：新增 `use_minute_execution` 开关，约 20 行改动
- `backtest/matcher.py`：不改，分钟执行器是 matcher 的上游替代
- **策略代码：零改动**

### 11.2 实盘：盘口感知下单

#### 现状问题

当前 `live_server.py` 下单时直接用当前价：

```python
# live_server.py submit_order()
result = self.broker.submit_order(
    ts_code=ts_code, side=side,
    quantity=quantity, price=price,  # price = 当前快照价
    ...
)
```

但当前快照价 ≠ 实际成交价。如果卖盘挂单不够，市价单会吃掉多层卖盘，实际成交价偏离可能很大。

#### 改造方案：下单前读盘口

```python
class OrderBookEstimator:
    """基于5档盘口预估成交价"""

    @staticmethod
    def estimate(quotes: dict, side: str, quantity: int) -> dict:
        """
        Args:
            quotes: TDX 实时行情 dict (含 bid1-5, ask1-5, bid_vol1-5, ask_vol1-5)
            side: 'BUY' or 'SELL'
            quantity: 目标股数
        Returns:
            {estimated_price, total_volume_available, enough_liquidity}
        """
        remaining = quantity
        total_cost = 0.0

        for level in range(1, 6):
            if side == 'BUY':
                price = quotes.get(f'ask{level}', 0)
                vol = quotes.get(f'ask_vol{level}', 0) * 100  # 手→股
            else:
                price = quotes.get(f'bid{level}', 0)
                vol = quotes.get(f'bid_vol{level}', 0) * 100

            if price <= 0 or vol <= 0:
                continue

            take = min(remaining, vol)
            total_cost += price * take
            remaining -= take

            if remaining <= 0:
                break

        filled = quantity - remaining
        avg_price = total_cost / filled if filled > 0 else 0

        return {
            'estimated_price': avg_price,
            'depth_available': quantity - remaining,
            'enough_liquidity': remaining <= 0,
            'slippage_from_best': (avg_price / quotes[f'ask1'] - 1) * 100 if side == 'BUY' and quotes.get('ask1') else 0,
        }
```

#### live_server.py 集成

```python
# scan_and_trade() 下单前
if self.trade_mode == 'auto':
    # 获取盘口估算
    quotes = self.fetcher.get_realtime_quotes([signal.ts_code])
    if quotes:
        est = OrderBookEstimator.estimate(quotes[signal.ts_code], 'BUY', quantity)
        if not est['enough_liquidity']:
            logger.warning(f"盘口流动性不足: 需要{quantity}股, 盘口仅{est['depth_available']}股")
            # 降级为限价单 或 降低数量
            quantity = max(est['depth_available'], 100)
        # 用预估价格代替快照价
        price = est['estimated_price']
    else:
        price = signal.price  # 降级: 用快照价

    result = self.submit_order(...)
```

**影响范围：**
- 新增 `broker/order_book.py`（约 50 行，OrderBookEstimator 类）
- `live_server.py`：`scan_and_trade()` 下单前约 10 行改动
- **策略代码：零改动**

### 11.3 回测 vs 实盘收益提升

| 场景 | 改进前 | 改进后 |
|------|--------|--------|
| **回测滑点** | 固定 0.2%（拍脑袋） | 基于历史分钟线 VWAP（真实） |
| **回测流动性** | 假设总能成交 | 分钟线量不够 → 部分成交/不成交 |
| **实盘下单** | 快照价直接下单 | 5 档盘口估算真实成交价 |
| **实盘流动性** | 发现不够已经晚了 | 下单前就知道盘口深度是否够 |

---

## 12. 数据流与定时同步

### 12.1 现状：被动入库

当前系统数据只在两个时机入库：

```
9:00  盘前 → 获取5分钟线，存 DB
15:00 收盘 → AKShare 获取今日日线，存 DB
其他时间 → 只读不写
```

**问题：**
- 盘中实时行情拿了就用，不存 → 回测看不到当日盘中波动
- 首次启动如果 DB 为空，必须等 AKShare 慢速拉取
- AKShare 挂了 → 系统无法初始化

### 12.2 改造后：获取即入库

TDX 数据获取极快（毫秒级），可以做到**每次获取都同步写入 DB**：

```
数据源获取 → 返回给调用方 → 同时异步写入 SQLite
```

#### 各数据类型入库策略

| 数据类型 | 入库时机 | 频率 | 数据源 |
|----------|----------|------|--------|
| **股票列表** | 服务启动 + 每周一刷新 | 周 | TDX（0.09s 全量） |
| **日K线** | 收盘后（15:30）批量增量 | 日 | TDX 为主 |
| **分钟线（当日）** | 收盘后全量保存（240 根） | 日 | TDX |
| **分钟线（历史）** | 首次全量 + 每日增量 | 一次性 | TDX |
| **除权除息** | 服务启动时全量 + 每日检查 | 日 | TDX |
| **板块成分** | 服务启动时全量 | 周 | TDX |
| **财务数据** | 每日收盘后增量更新 | 日 | TDX + AKShare |
| **实时行情/盘口** | ❌ 不存 | — | 实时消费 |
| **分笔成交** | ❌ 不存 | — | 实时消费 |

### 12.3 同步时序

```
                        交易日流程
===========================================================
09:00  启动 / 盘前
       ├── TDX 连接检查（自动选服务器）
       ├── 交易日历 → 增量同步
       ├── 除权除息 → 增量同步（全市场约 100 万条，首次 5 分钟）
       ├── 板块成分 → 全量同步（约 200 万条，5 分钟）
       └── 日线数据 → 检查缺失日期，补全

09:30  盘中扫描（每 60s）
       ├── TDX 实时行情 → 不存，直接用
       ├── TDX 5档盘口 → 不存，下单前查
       └── 策略信号 → 结构化日志（已在 live_trading.log）

15:00  收盘处理
       ├── TDX 当日日线 → upsert_daily_bars（全量股票池）
       ├── TDX 当日分钟线 → upsert_minute_bars（240 根 × N 只）
       ├── TDX 财务数据 → 增量 upsert_finance_detail
       └── 账户快照 → save_account_snapshot

周末   全量对账
       ├── 日K线全量校验（对比 DB vs TDX，补缺失）
       └── 分钟线全量校验
```

### 12.4 DataSyncService

新增 `data/sync_service.py`，负责所有定时同步逻辑：

```python
class DataSyncService:
    """数据同步服务 — 管理 TDX/AKShare 数据到 DB 的定时同步"""

    def __init__(self, db: SQLiteManager, source: BaseDataSource):
        self.db = db
        self.source = source

    def sync_daily_bars(self, codes: list, start: str, end: str):
        """批量获取日线并入库（增量）"""
        for code in codes:
            try:
                df = self.source.get_daily_data(code, start, end)
                if not df.empty:
                    rows = df.to_dict('records')
                    self.db.upsert_daily_bars(rows)
            except Exception as e:
                logger.warning(f'{code} 日线同步失败: {e}')

    def sync_minute_bars(self, codes: list, date: str, period: str = '5'):
        """同步当日分钟线"""
        ...

    def sync_xdxr(self, codes: list):
        """全量同步除权除息"""
        ...

    def sync_blocks(self):
        """全量同步板块成分股"""
        ...

    def sync_stock_list(self):
        """同步股票列表"""
        ...

    def check_and_fill_gaps(self, codes: list):
        """检查缺失日期，自动补全"""
        ...
```

### 12.5 更新后的实施步骤

| 步骤 | 内容 | 预计耗时 |
|------|------|----------|
| 1 | 创建 `data/sources/` 目录和 `base.py` 抽象基类 | 30 分钟 |
| 2 | 实现 `TDXDataSource`（核心方法） | 1.5 小时 |
| 3 | 迁移现有逻辑到 `AKShareDataSource` | 1 小时 |
| 4 | 改造 `DataFetcher` 为门面模式 + 自动降级 | 30 分钟 |
| 5 | 新增 `config/settings.py` 配置 | 10 分钟 |
| 6 | 数据库升级：3 张新表 + 2 张表扩展 | 30 分钟 |
| 7 | SQLiteManager 新增 upsert/查询方法（7 个） | 30 分钟 |
| 8 | 新增 `data/sync_service.py` 数据同步服务 | 1 小时 |
| 9 | 新增 `backtest/minute_executor.py` 分钟线执行器 | 1 小时 |
| 10 | 新增 `broker/order_book.py` 盘口估算器 | 30 分钟 |
| 11 | `backtest/engine.py` 集成分钟执行器 | 30 分钟 |
| 12 | `live_server.py` 集成盘口下单 | 20 分钟 |
| 13 | `server.py` `market_scheduler` 改为使用 sync_service | 40 分钟 |
| 14 | 测试验证 | 30 分钟 |

**总预计耗时：约 9.5 小时**

# 股票异动检测 — 六模块设计方案 (v2)

## 一、架构概览

```
TDXQuotesPoller (5秒轮询, 已有)
    │
    ├── get_security_quotes ──→ price, volume, bid1-5, ask1-5, active1, active2
    │                              │
    │                              ├──→ 内外盘背离检测 (新增)
    │                              ├──→ 盘口异动检测   (新增, 四维)
    │                              ├──→ 涨跌停加速检测 (新增)
    │                              └──→ 换手率异动检测 (新增)
    │
    ├── get_transaction_data ──→ 逐笔大单检测 (新增, 独立3秒线程)
    │
    └── get_finance_info ──→ liutongguben 缓存 ──→ 换手率计算

现有 MonitorEngine (MAD大单)
    │
    └──→ 新增 6 个 Detector, 共用 SSEManager + Alert + 名称缓存
```

### 代码结构

```
broker/
├── monitor.py              (不改)
│   └── Alert, QuoteSnapshot, TDXQuotesPoller, Detector(MAD),
│       SSEManager, MonitorEngine
│
├── detector/
│   ├── __init__.py
│   ├── trans_big.py        (逐笔大单)
│   ├── orderbook.py        (盘口异动)
│   ├── divergence.py       (内外盘背离)
│   ├── turnover.py         (换手率异动)
│   └── limit_move.py       (涨跌停加速)
│
└── market_watcher.py       (不改)
    └── + 排名突变 可加到此模块
```

### 统一原则

- 所有阈值基于**自身历史中位数**（MAD 范式），不用固定数值
- 告警统一用 `Alert` 数据类，`level` 字段区分类型
- 前端 `web/live.html` 不需改动，自然流入大单监控 Tab
- 同股票同类型告警 30 秒冷却（盘口单独 30 秒）

---

## 二、逐笔大单

### 数据来源

`get_transaction_data(market, code, start=0, count=10)`  
pytdx 返回：`{'time': '14:32', 'price': 1215.0, 'vol': 80, 'num': 15, 'buyorsell': 2}`  
buyorsell: 1=买 2=卖 8=集合竞价

### 频率

独立线程，**3 秒**轮询。但不扫全部 81 只——先过滤：
```
volume 增量 > MAD×3 的股票 → 才拉逐笔
```
减少 TDX QPS 压力。

### 阈值

```
绝对下限: 2000万元 (游资/机构级别)
动态线:   该股近30日逐笔成交额的中位数 × 30

触发条件: 单笔成交额 > max(2000万, 动态线)
```

### 集合竞价处理

不简单忽略（buyorsell=8），而是对比历史同时段：

```
今日 9:25 竞价成交量 vs 近5日 9:25 成交量中位数
超 3 倍 → 标记 "auction_spike" (竞价异常放量)
正常范围 → 不标记
```

收盘竞价(15:00)同理。

### 分档

```
大单:   单笔 > 阈值
特大单: 单笔 > 阈值 × 3 且 > 5000万
巨单:   单笔 > 1亿
```

### Alert

```json
{
  "type": "trans_big",
  "code": "600519", "name": "贵州茅台",
  "level": "super_large",
  "direction": "buy",
  "price": 1215.0,
  "amount": 52000000,
  "hands": 428,
  "threshold": 20000000,
  "multiple": 2.6,
  "time": "14:32:15"
}
```

---

## 三、盘口异动

### 数据来源

复用 `get_security_quotes` 的五档盘口（每 5 秒一轮已有数据）。

### 四维检测

#### 3.1 挂单突变

```
条件: 任一档(买1-5/卖1-5) 挂量 > 该档前5分钟中位数×10  且 挂量 > 2000手
含义: 突然有大单挂出来了 → 有人要动手
冷却: 30秒
```

#### 3.2 盘口失衡

```
买盘总量 = sum(bid_vol1-5)
卖盘总量 = sum(ask_vol1-5)
比率 = 买盘总量 / 卖盘总量

严重失衡: 比率 < 0.2 或 > 5  且 大的一方 > 5000手
含义: 买卖盘结构极端倾斜
```

#### 3.3 大单撤单

```
上一拍: 任一档挂量 > 该档中位数×10 且 > 2000手
当前拍: 同一档挂量 < 200手
→ 撤单: 大单消失 → 假单误导
```

#### 3.4 价差突变

```
spread = ask1 - bid1
spread_pct = spread / bid1 × 100

突变: spread_pct > 0.5%  且  > 近5分钟均值 × 5
含义: 流动性突然枯竭
```

### Alert

```json
{
  "type": "orderbook",
  "code": "600519",
  "subtype": "imbalance",
  "level": "severe",
  "bid_total": 12800,
  "ask_total": 2800,
  "ratio": 4.57,
  "price": 1215.0,
  "time": "14:32:20"
}
```

---

## 四、内外盘背离

### 数据来源

`get_security_quotes` 返回 `active1`(外盘/主动买) 和 `active2`(内盘/主动卖)。

### 三种子检测

#### 4.1 价涨内盘大

```
条件: 当前price > 5秒前price  且  active_sell > active_buy × 1.5
含义: 价格涨但卖盘更多 → 诱多嫌疑
```

#### 4.2 价跌外盘大

```
条件: 当前price < 5秒前price  且  active_buy > active_sell × 1.5
含义: 价格跌但买盘更多 → 压价吸筹嫌疑
```

#### 4.3 极端背离

```
比率 = max(active_buy, active_sell) / min(active_buy, active_sell)
条件: 比率 > 3:1  且  持续 ≥ 3拍 (15秒)
含义: 持续内外盘极端不对等
```

### Alert

```json
{
  "type": "divergence",
  "code": "600519",
  "subtype": "price_up_sell_more",
  "price": 1220.0,
  "price_delta_pct": 0.8,
  "active_buy": 12000,
  "active_sell": 20000,
  "ratio": 0.6,
  "duration": 15,
  "time": "14:32:25"
}
```

---

## 五、换手率异动

### 数据来源

- 成交量: `get_security_quotes.vol`（累计，每5秒拍一次）
- 流通股本: `get_finance_info` → `liutongguben`（启动时一次性缓存，全天复用）

### 算法

基于**每只股票自身 1 个月历史换手率的中位数**：

```
日内换手率 = 累计成交量(手) × 100 / 流通股本(股) × 100

最近5分钟换手增量 = 
  当前累计换手率 - 5分钟前累计换手率

5分钟增量判定:
  5分钟换手 > 该股日均5分钟换手中位数 × 5  → "turnover_spike"

全天累计判定:
  全天换手 > 该股历史中位数 × 3  → "turnover_hot"  
  全天换手 > 该股历史中位数 × 5  → "turnover_extreme"
```

### 历史中位数参考（实测数据）

基于 81 只股票池近 60 日原始数据：

| 区间 | 股票数 | 代表 |
|------|------|------|
| <1% | 15 | 工行(0.1%) 农行(0.1%) 茅台(0.3%) 招行(0.3%) |
| 1-3% | 15 | 宁德(0.8%) 比亚迪(1.2%) 五粮液(0.6%) 格力(0.7%) |
| 3-7% | 25 | 科技/通信/制造 |
| >7% | 26 | 603629(13%) 300136(12%) 603083(11.5%) |

### Alert

```json
{
  "type": "turnover",
  "code": "002415",
  "subtype": "spike",
  "daily_turnover_pct": 8.5,
  "delta_5m_pct": 2.3,
  "median_5m": 0.4,
  "multiple": 5.75,
  "price": 35.6,
  "time": "14:32:30"
}
```

---

## 六、涨跌停加速

### 涨跌停价格

启动时用 `last_close` 计算并缓存：

```
主板(60xxxx/00xxxx): ±10%
科创板(688xxx):      ±20%
创业板(300xxx):      ±20%
```

### 三种子检测

#### 6.1 封板松动

```
条件: 涨跌幅 > 9.5%  且  买1挂量 < 前5分钟买1挂量中位数 × 0.5
含义: 涨停封单骤降 → 可能开板
```

#### 6.2 撬板信号

```
条件: 涨跌幅 < -9.5%  且  买1挂量突然 > 卖1挂量 × 10
含义: 跌停板出现大量买单 → 可能撬开
```

#### 6.3 逼近加速

```
条件: |涨跌幅| > 8%  且  
      (price与前5分钟相比) 的变化>3%  且
      5分钟换手增量 > 3倍
含义: 加速冲板/跌停
```

### Alert

```json
{
  "type": "limit_move",
  "code": "603083",
  "subtype": "seal_loosen",
  "price": 45.2,
  "change_pct": 9.8,
  "bid1_vol": 1200,
  "bid1_vol_median": 35000,
  "drop_ratio": 0.034,
  "time": "14:32:35"
}
```

---

## 七、涨跌幅排名突变

### 范围

仅 81 只自选池（全市场扫描 QPS 过大）。

### 算法

```
每次 poll 后: 自选股按 change_pct 排序

触发:
  排名跃升 > 30 位 (81只中)  → "rank_surge"
  涨幅从 <2% 突变为 >5% (绝对阈值) → "pct_jump"
```

### Alert

```json
{
  "type": "rank_change",
  "code": "002156",
  "subtype": "rank_surge",
  "rank_before": 48,
  "rank_after": 12,
  "jump": 36,
  "change_pct": 5.8,
  "time": "14:32:40"
}
```

---

## 八、MonitorEngine 集成

```python
# MonitorEngine._poll_loop() 每5秒一轮

snapshots = self._poller.poll(self._stock_pool)

alerts = []

# 1. 现有 MAD 大单检测 (不改)
alerts += self._mad_detector.check(snapshots, prev_snapshots)

# 2. 内外盘背离 (同批 snapshots)
alerts += self._divergence_detector.check(snapshots, prev_snapshots)

# 3. 盘口异动 (同批 snapshots)
alerts += self._orderbook_detector.check(snapshots)

# 4. 涨跌停加速 (同批 snapshots + limit_prices 缓存)
alerts += self._limit_move_detector.check(snapshots)

# 5. 换手率异动 (同批 snapshots + liutongguben 缓存)
alerts += self._turnover_detector.check(snapshots)

# 6. 排名突变 (同批 snapshots, 排序前后对比)
alerts += self._rank_detector.check(snapshots)

# 7. 逐笔大单 (独立 3s 线程, 结果通过队列合并)
trans_alerts = self._trans_queue.get_all_nonblocking()
alerts += trans_alerts

# 统一推送
for alert in alerts:
    self._sse.push(alert)
```

### 启动时初始化

```python
def start(self, stock_pool):
    # ... 现有逻辑 ...
    
    # 缓存流通股本 (换手率需要)
    self._liutong_cache = self._load_liutongguben(stock_pool)
    
    # 缓存涨跌停价格
    self._limit_prices = self._calc_limit_prices(stock_pool)
    
    # 启动逐笔大单线程 (独立 3s)
    self._trans_thread = threading.Thread(target=self._trans_loop, daemon=True)
    self._trans_thread.start()
```

### 前端兼容

`web/live.html` 不改。新告警类型通过 `level` 字段区分：

| 已有 | 新增 |
|------|------|
| `super_large` | `trans_big` (逐笔大单) |
| `large` | `orderbook_anomaly` (盘口) |
| `volume_spike` | `divergence` (内外盘背离) |
| | `turnover_spike` / `turnover_hot` / `turnover_extreme` |
| | `limit_move` (涨跌停) |
| | `rank_change` (排名) |
| | `auction_spike` (竞价异常) |

筛选按钮新增对应 label 即可。

---

## 九、决策记录

### Q1 — 集合竞价

**问：逐笔大单中，集合竞价（buyorsell=8，9:25/15:00）是否忽略？每天都会有一笔巨量撮合，标记的话每天都刷屏。**

讨论：可以对比近5日同时间集合竞价成交量中位数，而非简单忽略。

> **决策：对比近5日同时段（9:25 / 15:00）竞价成交量中位数，超 3 倍才报"竞价异常放量"。正常范围不标记。**

---

### Q2 — 逐笔绝对下限

**问：逐笔大单的绝对下限定多少？统一 50 万对茅台（4手）太敏感，对银行股（1000手）又太迟钝。按股价分段还是按市值？**

讨论：游资一般在 500万~3000万，机构在 1000万以上。1000万对茅台≈8手、工行≈2000手，都合理。用户最终选 2000万只盯游资/机构级别。

> **决策：绝对下限 2000万，配合动态线 = 该股近30日均笔额 × 30。**

---

### Q3 — 逐笔检测频率

**问：`get_transaction_data` 每次只返回最近10笔。5秒轮询可能漏掉中间几十笔成交。2秒、3秒对程序有影响吗？**

讨论：2秒会导致 TDX QPS 从 16/s 升到 ~56/s，可能被踢。3秒更安全（~43/s）。另外可优化：只对有成交异动的股票（volume增量 > MAD×3）才拉逐笔，而不是全扫 81 只。

> **决策：独立线程 3 秒轮询，先过滤异动股（volume增量 > MAD×3）再查逐笔。**

---

### Q4 — 盘口异动方案与冷却

**问：盘口异动只看买1卖1还是保留四维（挂单突变/盘口失衡/撤单/价差突变）？冷却 30 还是 60 秒？**

讨论：
- 只看买1卖1会与已有的 SurgeWatcher（价格跳变）和 MAD 大单（成交异动）重合
- 四维盘口检测的是"还没成交但正在发生的事"——挂单意图、盘口结构、欺诈、流动性，与现有功能不重叠
- 触发条件本身很高（>2000手且>10倍中位数等），实际每天每只最多1-3次，30秒完全不会刷屏

> **决策：保留四维盘口方案，同股票同类型30秒冷却。**

---

### Q5 — 整体轮询频率

**问：监控轮询保持5秒还是降到3秒？**

讨论：盘口异动底层复用同一批 snapshots，不增加额外请求。逐笔有独立线程，不受影响。3秒会多 60% QPS 但不会多捕获几个异动（异动本身就罕见）。5秒稳妥。

> **决策：主线轮询保持 5 秒。**

---

### Q6 — 换手率阈值

**问：换手率异动用固定阈值还是按市值分档？**

实际数据分析：81只股票池换手率从工行 0.1% 到 603629 的 13%，差距 130 倍。统一阈值行不通。建议用每只股票自身 1 个月历史换手中位数，像 MAD 大单检测一样——自身对比自身。

> **决策：每只股票用自身 1 个月历史换手中位数。5分钟增量 > 自身中位数 × 5 为异动；全天累计 > 自身中位数 × 3 为放量，> × 5 为极端。**

---

### Q7 — 涨跌停价格

**问：涨跌停价格是启动时算一次缓存，还是每轮 poll 重算？**

讨论：涨跌停价 = last_close × (1 ± 10%/20%)，昨收全天不变。

> **决策：启动时计算一次，缓存，全天复用。**

---

### Q8 — 排名突变范围

**问：涨跌幅排名突变只扫81只自选池，还是全市场6910只？**

讨论：全市场需要每30秒轮询6910只股票，QPS 到 230/s，TDX 公共服务器可能扛不住。

> **决策：只做自选池 81 只。排名跃升 > 30 位或涨幅从 <2% 突升到 >5% 触发。**

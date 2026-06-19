# 股票异动检测六模块 — 执行规划

## Context

根据 `docs/anomaly_detection_design.md` 设计方案，需要为现有 MonitorEngine 新增 6 个异动检测模块：
逐笔大单、盘口异动、内外盘背离、换手率异动、涨跌停加速、排名突变。

### 现状摘要

| 已有资产 | 位置 | 可复用方式 |
|----------|------|-----------|
| `MonitorEngine` 单例 + `_poll_loop` | `broker/monitor.py:530-869` | 集成点，新增 detector 调用 |
| `SSEManager.push()` | `broker/monitor.py:441-496` | SSE 推送不变 |
| `TDXQuotesPoller.poll()` | `broker/monitor.py:104-192` | 需扩展以填充五档盘口数据 |
| `QuoteSnapshot` frozen dataclass | `broker/monitor.py:79-96` | 需扩展：新增 bid2-5/ask2-5 及其挂量 + last_close |
| `Alert` frozen dataclass | `broker/monitor.py:50-71` | **不用改** — 新告警用独立的 `AnomalyAlert` |
| `MarketWatcherEngine` 子模块模式 | `broker/market_watcher.py` | 参考其 `process(snapshots) -> List[dict]` + fault isolation |
| `Detector` MAD 模式 | `broker/monitor.py:297-433` | 阈值计算方法参考 |
| `web/monitor_demo.html` | 设计参考 | 标签过滤 UI 模板 |

### 关键约束

1. **QuoteSnapshot 缺少五档盘口**：TDX 数据源 (`tdx_source.py:337-356`) 已返回 `bid_vol1-5`、`ask_vol1-5`，但 `QuoteSnapshot` 只捕获了 `bid1`/`ask1` 价格，缺挂量。盘口检测器依赖这些字段。
2. **Alert 过于僵硬**：现有 `Alert` 有 15 个 MAD 专用字段 (`super_large_flow`、`mad_multiple` 等)，无法容纳 7 种新告警类型。需新建灵活的 `AnomalyAlert`。
3. **逐笔大单需独立线程**：3 秒轮询 + 预筛选避免 QPS 爆炸。

---

## 执行阶段

### 第 0 步：创建目录结构

**新建文件：**
```
broker/detector/__init__.py
broker/detector/divergence.py
broker/detector/orderbook.py
broker/detector/limit_move.py
broker/detector/turnover.py
broker/detector/trans_big.py
```

### 第 1 步：Foundation — 数据模型 + 基础设施

**1a. 扩展 `QuoteSnapshot`** (`broker/monitor.py:79-96`)
- 新增字段（全部带默认值 `0.0`，保持向后兼容）：
  - `bid2, bid3, bid4, bid5, ask2, ask3, ask4, ask5`（价格）
  - `bid_vol1-5, ask_vol1-5`（挂量）
  - `last_close`（昨收）

**1b. 更新 `TDXQuotesPoller.poll()`** (`broker/monitor.py:167-182`)
- 构造 `QuoteSnapshot` 时填充新增的五档盘口字段

**1c. 创建 `broker/detector/__init__.py`**
- `AnomalyAlert` dataclass：`type`, `subtype`, `code`, `name`, `time`, `timestamp`, `data: dict`
- `SimpleQueue`：线程安全非阻塞队列（供逐笔大单跨线程传告警）
- 导出所有 detector class

**1d. 添加 `ANOMALY_DETECTOR_CONFIG`** (`config/settings.py`)
- 6 个检测器的所有阈值参数（详见设计文档 Q1-Q8 决策）

### 第 2 步：无 DB 依赖的检测器（可并行开发）

每个 detector 遵循 `market_watcher.py` 模式：
- 构造函数接受阈值参数
- `check(curr, prev?) -> List[AnomalyAlert]`
- 独立 logger + fault isolation

**2a. `divergence.py` — 内外盘背离**
- 输入：`active_buy`, `active_sell`, `price`（已有字段）
- 三个子检测：价涨内盘大、价跌外盘大、极端背离(持续3拍)
- 冷却：30s/股票/子类型
- 需维护：每股票最近 5 拍的 active 差值窗口

**2b. `orderbook.py` — 盘口异动（四维）**
- 输入：五档盘口挂量（需 1a 完成）
- 四个子检测：
  1. 挂单突变：任一档挂量 > 中位数×10 且 > 2000手
  2. 盘口失衡：买卖比 < 0.2 或 > 5，大的一方 > 5000手
  3. 大单撤单：上拍有巨量挂单，下拍消失(<200手)
  4. 价差突变：spread_pct > 0.5% 且 > 5分钟均值×5
- 需维护：每股票每档 60s(12拍) 挂量窗口 + 上一拍快照

**2c. `limit_move.py` — 涨跌停加速**
- 输入：涨跌幅、bid1_vol、ask1_vol、价格变化、换手增量
- 启动时缓存涨跌停价（`last_close × (1 ± 10%/20%)`）
- 三个子检测：封板松动、撬板信号、逼近加速

### 第 3 步：有 DB 依赖的检测器

**3a. `turnover.py` — 换手率异动**
- 输入：`volume`(累计)、`liutongguben`(流通股本缓存)
- 算法：日内换手率 = 累计成交量(手) × 100 / 流通股本(股) × 100
- 5分钟增量 > 股票自身5分钟中位数 × 5 → `turnover_spike`
- 全天累计 > 自身中位数 × 3 → `turnover_hot`，× 5 → `turnover_extreme`
- 启动时从 DB `finance_detail` 加载 `float_shares` + 从 `daily_bars` 计算历史中位数

**3b. `trans_big.py` — 逐笔大单（独立线程）**
- 独立 daemon 线程，3 秒轮询
- 预筛选：只对 volume 增量 > MAD×3 的股票调用 `get_transaction_data`
- 阈值：`max(2000万, 近30日逐笔成交额中位数 × 30)`
- 分档：大单 > 阈值，特大单 > 阈值×3 且 > 5000万，巨单 > 1亿
- 集合竞价：对比近5日同时段中位数，超3倍 → `auction_spike`
- 结果通过 `SimpleQueue` 非阻塞传递给主循环

### 第 4 步：集成到 MonitorEngine

**修改 `broker/monitor.py`：**

**4a. `__init__`** — 创建 6 个 detector 实例 + `SimpleQueue`

**4b. `start()`** — 新增：
- `_load_liutongguben_cache()` — 从 DB 加载流通股本
- `_compute_detector_hist_medians()` — 计算换手率/逐笔历史中位数
- `_load_limit_prices()` — 计算涨跌停价格
- 启动 `trans_big` daemon 线程

**4c. `_poll_loop()`** — 在现有 MAD 检测后插入（每个用 try/except 包裹）：
```python
# 2. 内外盘背离
alerts += self._divergence_detector.check(curr, prev)
# 3. 盘口异动
alerts += self._orderbook_detector.check(curr)
# 4. 涨跌停加速
alerts += self._limit_move_detector.check(curr)
# 5. 换手率异动
alerts += self._turnover_detector.check(curr)
# 6. 排名突变
alerts += self._rank_detector.check(curr)
# 7. 逐笔大单 (从队列取)
alerts += self._trans_queue.get_all_nonblocking()
```
- 新告警统一通过 `self._sse.push(anomaly_alert)` 推送（`AnomalyAlert.to_sse_data()` 与现有 SSE 协议兼容）

**4d. `stop()`** — 停止 trans_big 线程

### 第 5 步：排名突变检测（内嵌，不独立文件）

- 逻辑简单（排序 + 对比前后排名），直接在 MonitorEngine 中实现或作为轻量 detector
- 只扫 81 只自选池，排名跃升 > 30 位或涨幅从 <2% 突变到 >5%

### 第 6 步：前端适配

**修改 `web/live.html`：**
- `renderAlerts()` 识别新的 `AnomalyAlert` 格式（`a.type` + `a.subtype` + `a.data`）
- 添加告警类型过滤按钮（参考 `monitor_demo.html` 的标签系统）
- 类型到图标/颜色的映射：
  - `trans_big` → 🟣 逐笔大单
  - `orderbook` → 🟠 盘口异动
  - `divergence` → 🟢 内外盘背离
  - `turnover` → 🔵 换手率异动
  - `limit_move` → 🔴 涨跌停异动
  - `auction_spike` → ⚪ 竞价异常
  - `rank_change` → 🔷 排名突变

### 第 7 步：测试

**新建 `tests/unit/test_anomaly_detectors.py`**，参考 `test_market_watcher.py` 模式：

| ID | 检测器 | 用例 |
|----|--------|------|
| TC-AD01 | Divergence | 价涨内盘大触发 |
| TC-AD02 | Divergence | 正常无触发 |
| TC-AD03 | Divergence | 极端背离持续触发 |
| TC-AD04 | Orderbook | 挂单突变触发 |
| TC-AD05 | Orderbook | 盘口失衡触发 |
| TC-AD06 | Orderbook | 撤单检测 |
| TC-AD07 | Orderbook | 价差突变 |
| TC-AD08 | LimitMove | 封板松动 |
| TC-AD09 | LimitMove | 撬板信号 |
| TC-AD10 | Turnover | 换手率骤增 |
| TC-AD11 | TransBig | 分档分类正确 |
| TC-AD12 | TransBig | 竞价异常检测 |
| TC-AD13 | QuoteSnapshot | 新字段填充正确 |
| TC-AD14 | AnomalyAlert | JSON 序列化正确 |

---

## 验证方案

1. **单元测试**：`python -m pytest tests/unit/test_anomaly_detectors.py -v`
2. **集成测试**：启动 `python server.py`，确认：
   - MonitorEngine 正常初始化（detector 创建 + 缓存加载）
   - 逐笔线程正常启动和停止
   - SSE 推送新告警类型，前端正常接收
3. **前端验证**：打开 `web/live.html` 监控 Tab，观察新告警类型渲染和过滤
4. **回归验证**：`python -m pytest tests/ -v` 确保已有测试全过

---

## 文件变更清单

| 操作 | 文件 |
|------|------|
| 新建 | `broker/detector/__init__.py` |
| 新建 | `broker/detector/divergence.py` |
| 新建 | `broker/detector/orderbook.py` |
| 新建 | `broker/detector/limit_move.py` |
| 新建 | `broker/detector/turnover.py` |
| 新建 | `broker/detector/trans_big.py` |
| 新建 | `tests/unit/test_anomaly_detectors.py` |
| 修改 | `broker/monitor.py` (QuoteSnapshot 扩展 + TDXQuotesPoller + MonitorEngine 集成) |
| 修改 | `config/settings.py` (ANOMALY_DETECTOR_CONFIG) |
| 修改 | `web/live.html` (新告警类型渲染 + 过滤) |

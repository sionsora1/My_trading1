# 盯盘模块 — 执行方案

> 版本: v1.0 | 日期: 2026-06-17 | 预计总耗时: 7 小时

---

## 1. 编码规范（所有子 Agent 强制执行）

### 1.1 命名规范

| 类型 | 规范 | 示例 |
|------|------|------|
| 类名 | PascalCase | `SurgeWatcher`, `MarketWatcherEngine` |
| 方法名 | snake_case | `process()`, `_push_alerts()` |
| 私有方法 | 前缀 `_` | `_poll_loop()`, `_scan_limit_stocks()` |
| 常量 | UPPER_SNAKE | `DEFAULT_INTERVAL`, `MAX_ALERTS` |
| 布尔变量 | is_ / has_ 前缀 | `is_running`, `has_data` |
| 模块名 | snake_case | `market_watcher.py` |

### 1.2 注释规范

- 每个类必须有 docstring，说明职责和数据流
- 每个 public 方法必须有 docstring，包含 Args / Returns
- 复杂逻辑用行内注释，标注 "为什么" 而非 "做什么"
- 中文注释用于业务逻辑说明

```python
class SurgeWatcher:
    """异动拉升/跳水检测。

    维护 60 秒滑动窗口，每次 poll 后比较最新价与窗口起始价，
    涨跌幅超过阈值时触发告警。触发后清空窗口避免重复。
    """

    def process(self, curr: Dict[str, QuoteSnapshot]) -> List[dict]:
        """处理本轮快照，返回异动告警列表。

        Args:
            curr: {code: QuoteSnapshot}  当前轮快照

        Returns:
            [{"type": "surge_up", "code": ..., "name": ..., ...}, ...]
        """
```

### 1.3 异常处理规范

```python
# 对外部依赖（TDX / DB）必须 try/except + 日志
try:
    api.connect(ip, port)
except Exception as e:
    logger.error(f"TDX连接失败: {e}", extra={"data": {"ip": ip}})
    return []  # 降级返回空，不崩主循环

# 轮询循环必须全局兜底
while self._running.is_set():
    try:
        # ... 所有业务逻辑 ...
    except Exception as e:
        logger.error(f"轮询循环异常: {e}", exc_info=True)
        time.sleep(10)

# 绝不用裸 except: pass
# 至少记录 debug 日志
```

### 1.4 日志规范

```python
from utils.logger import get_logger
logger = get_logger('live_trading', 'live_trading.log')

# INFO: 业务事件（启动/停止/告警）
logger.info("异动拉升", extra={"data": {"code": "600519", "change_pct": 2.1}})

# WARNING: 可恢复异常（连接失败降级、数据为空）
logger.warning("板块热点获取失败, 使用缓存", extra={"data": {"error": str(e)}})

# ERROR: 不可恢复异常（需要 exc_info=True 记录堆栈）
logger.error(f"轮询失败: {e}", exc_info=True)

# DEBUG: 调试信息（心跳、中间状态、数据摘要）
logger.debug(f"涨跌停扫描完成", extra={"data": {"elapsed_s": 28.5}})
```

### 1.5 类型注解规范

```python
from typing import Dict, List, Optional
# 所有公开方法参数和返回值必须有类型注解
def process(self, curr: Dict[str, QuoteSnapshot]) -> List[dict]:
```

---

## 2. 任务拆分

### 子 Agent A: `broker/market_watcher.py` — 引擎 + 前 3 个子模块

**文件：** `broker/market_watcher.py`（新建）

**内容：**
- `MarketWatcherEngine` 单例引擎（生命周期、轮询循环、SSE 推送）
- `PoolStatusTracker` 自选股状态
- `SurgeWatcher` 异动拉升
- `FlowWatcher` 主力资金流

**验收标准：**
```python
from broker.market_watcher import MarketWatcherEngine, PoolStatusTracker, SurgeWatcher, FlowWatcher
# 1. 引擎单例正常
e1 = MarketWatcherEngine.get_instance()
e2 = MarketWatcherEngine.get_instance()
assert e1 is e2

# 2. PoolStatusTracker 产出正确格式
tracker = PoolStatusTracker()
# mock snapshots...

# 3. SurgeWatcher 异动检测
sw = SurgeWatcher()
# 60秒窗口内价格涨2% → 触发 surge_up

# 4. FlowWatcher 资金流检测
fw = FlowWatcher()
# 1分钟内外盘净流入 > 5000万 → 触发 big_inflow
```

---

### 子 Agent B: `broker/market_watcher.py` — 后 2 个子模块

**文件：** `broker/market_watcher.py`（追加）

**内容：**
- `LimitUpDownWatcher` 全市场涨跌停扫描
- `SectorHeatmap` 板块热点

**验收标准：**
```python
from broker.market_watcher import LimitUpDownWatcher, SectorHeatmap
# 1. LimitUpDownWatcher 全市场扫描
lw = LimitUpDownWatcher()
alerts = lw.process()
assert isinstance(alerts, list)
# 2. SectorHeatmap 板块数据
sh = SectorHeatmap()
data = sh.process()
assert "concept" in data
assert "industry" in data
```

---

### 子 Agent C: `web/live.html` + `server.py` — 前端 + API

**文件：**
- `web/live.html`（修改）— 新增"盯盘"标签页
- `server.py`（修改）— 新增 4 个 API 端点

**API 端点：**
```python
POST /api/watcher/start   # 启动盯盘
POST /api/watcher/stop    # 停止盯盘
GET  /api/watcher/status  # 状态查询
GET  /api/watcher/stream  # SSE 实时推送
```

**前端要求：**
- 子标签：自选股 / 涨跌停 / 异动 / 板块 / 资金流
- 统计面板：今日涨停 N 只 | 跌停 N 只 | 异动 N 次
- 自选股按涨跌幅降序排列，红涨绿跌
- SSE 接收 6 种事件类型并渲染

---

### 子 Agent D: `tests/` + `tests/` — 单元测试

**文件：**
- `tests/unit/test_market_watcher.py`（新建）

**测试用例：**

| 编号 | 测试项 | 验证点 |
|------|--------|--------|
| TC-MW01 | MarketWatcherEngine 单例 | `get_instance()` 两次返回同一对象 |
| TC-MW02 | PoolStatusTracker 排序 | 快照按 change_pct 降序 |
| TC-MW03 | SurgeWatcher 触发 | 60秒内涨幅 2% 触发 surge_up |
| TC-MW04 | SurgeWatcher 不触发 | 60秒内涨幅 0.5% 不触发 |
| TC-MW05 | SurgeWatcher 防重复 | 触发后窗口清空 |
| TC-MW06 | FlowWatcher 流入 | 净流入 6000 万触发 big_inflow |
| TC-MW07 | FlowWatcher 流出 | 净流出 6000 万触发 big_outflow |
| TC-MW08 | FlowWatcher 不过滤 | 净流入 1000 万不触发 |
| TC-MW09 | LimitUpDownWatcher 扫描 | 返回列表类型 |
| TC-MW10 | SectorHeatmap 缓存 | 30秒内返回缓存数据 |
| TC-MW11 | 日志规范 | 所有类使用 get_logger |
| TC-MW12 | 类型注解 | 所有公开方法有完整类型注解 |

---

### 子 Agent E: Code Review（最后执行）

**审查维度：**

| 维度 | 检查项 |
|------|--------|
| 命名规范 | 类 PascalCase / 方法 snake_case / 私有 `_` 前缀 |
| 注释 | 每个类和方法有 docstring |
| 异常处理 | 外部依赖有 try/except / 轮询循环有全局兜底 / 绝无裸 except:pass |
| 日志 | 使用 get_logger / 告警 INFO + data / 错误 ERROR + exc_info |
| 类型注解 | 公开方法参数和返回值有类型 |
| 单例安全 | `get_instance()` 线程安全 / `__init__` 防重复 |
| 线程安全 | SSE 推送用 `run_coroutine_threadsafe` / Alert 列表用 `threading.Lock` |
| 降级策略 | 子模块异常不影响主循环 / 数据为空时返回空不崩溃 |
| 代码风格 | 单引号 / PEP8 / 无重复代码 / 无魔法数字 |

**审查输出：** 问题清单（文件:行号:问题描述），交给主 Agent 确认后修复。

---

## 3. 执行流程

```
Phase 1 (并行)
  子Agent A → Engine + PoolStatusTracker + SurgeWatcher + FlowWatcher
  子Agent B → LimitUpDownWatcher + SectorHeatmap

Phase 2 (并行, 依赖 A+B)
  子Agent C → live.html + server.py (需要知道 SSE 事件格式)
  子Agent D → 单元测试

Phase 3 (依赖 C+D)
  子Agent E → 代码审查 (审查所有文件)

Phase 4 (主Agent)
  集成验证 → 提交
```

每个子 Agent 完成后输出验收结果，主 Agent 确认通过后进入下一轮。

---

## 4. 主 Agent 监督清单

| 阶段 | 检查项 |
|------|--------|
| Phase 1 前 | A/B agent 理解需求、编码规范已同步 |
| Phase 1 后 | 所有方法符合接口定义、单例安全、日志规范 |
| Phase 2 前 | A/B 输出文件无冲突（追加到同一文件） |
| Phase 2 后 | 前端 5 个子标签 + API 4 个端点可用 |
| Phase 3 前 | C/D 输出完整 |
| Phase 3 后 | Review 问题清单，决定哪些修改哪些忽略 |
| Phase 4 | 导入不报错、盯盘启动不崩溃、日志正常输出 |

# 日志系统设计方案

## 1. 现状分析

### 当前问题

| 问题 | 影响 |
|------|------|
| 日志分散 | `print()` 和 `logger` 混用，输出不统一 |
| 无业务日志 | 信号生成、交易执行、风控判断等关键操作无记录 |
| 无日志轮转 | 日志文件无限增长，占用磁盘 |
| 无结构化日志 | 纯文本日志，难以检索和分析 |
| 实盘日志为空 | `live_server.log` 为 0 行，无法追溯问题 |

### 现有日志分布

```
server.log          ← uvicorn HTTP 访问日志（501行）
live_server.log     ← 实盘服务日志（0行，未使用）
live_server_error.log ← 错误日志（0行）
logs/               ← 目录未创建
utils/logger.py     ← 日志工具类（存在但未被引用）
```

---

## 2. 设计目标

1. **统一入口** — 所有日志通过 `utils/logger.py` 输出，禁止裸 `print()`
2. **分模块存储** — 实盘、回测、风控、数据 各自独立日志文件
3. **结构化格式** — JSON 格式，便于后续接入日志分析工具
4. **自动轮转** — 按文件大小/日期自动清理，防止磁盘占满
5. **关键操作必记** — 信号生成、下单、成交、风控拦截 必须有日志

---

## 3. 日志分类与级别

### 3.1 日志文件规划

```
logs/
├── server.log              ← Web 服务主日志（HTTP 请求 + 异常）
├── live_trading.log        ← 实盘交易日志（信号/下单/成交）
├── backtest.log            ← 回测执行日志
├── risk.log                ← 风控判断日志（拦截/放行/告警）
├── data.log                ← 数据获取日志（拉取/同步/异常）
└── error.log               ← 全局错误日志（所有 ERROR 级别汇总）
```

### 3.2 日志级别定义

| 级别 | 用途 | 示例 |
|------|------|------|
| `DEBUG` | 调试信息，生产环境关闭 | 变量值、中间状态 |
| `INFO` | 正常业务流程 | 信号生成、扫描完成、数据同步 |
| `WARNING` | 异常但可恢复 | 实时行情获取失败、降级处理 |
| `ERROR` | 错误需关注 | 策略执行异常、下单失败 |
| `CRITICAL` | 严重故障 | 数据库连接断开、风控熔断触发 |

---

## 4. 日志格式设计

### 4.1 控制台格式（人类可读）

```
2026-06-15 14:30:25 [INFO] [实盘] 信号生成 | 策略=trend_following | 股票=600519 | 方向=BUY | 权重=0.15 | 原因=趋势跟随（得分0.20）
```

### 4.2 文件格式（JSON 结构化）

```json
{
  "timestamp": "2026-06-15T14:30:25.123456",
  "level": "INFO",
  "module": "live_trading",
  "action": "signal_generated",
  "message": "信号生成",
  "data": {
    "strategy": "trend_following",
    "ts_code": "600519.SH",
    "signal": "BUY",
    "weight": 0.15,
    "reason": "趋势跟随（得分0.20）",
    "price": 1262.98
  }
}
```

---

## 5. 关键日志点设计

### 5.1 实盘交易日志 (`live_trading.log`)

| 事件 | 级别 | 记录内容 |
|------|------|----------|
| 服务启动/停止 | INFO | 启动时间、配置、模式 |
| 策略扫描开始 | INFO | 扫描时间、股票池数量、策略列表 |
| 信号生成 | INFO | 策略、股票代码、方向、权重、原因 |
| 信号被过滤 | WARNING | 过滤原因（涨跌停/持仓上限/金额不足） |
| 下单请求 | INFO | 股票代码、方向、数量、价格、金额 |
| 下单结果 | INFO | 成功/失败、订单号、错误信息 |
| 成交回报 | INFO | 成交价、成交数量、手续费 |
| 风控拦截 | WARNING | 拦截原因（日亏损/单股超限） |
| 扫描异常 | ERROR | 异常类型、堆栈 |

### 5.2 回测日志 (`backtest.log`)

| 事件 | 级别 | 记录内容 |
|------|------|----------|
| 回测任务提交 | INFO | 任务ID、股票池、时间范围、策略 |
| 数据加载 | INFO | 数据来源（DB/AKShare）、股票数、耗时 |
| 回测完成 | INFO | 任务ID、总收益、交易次数、耗时 |
| 回测失败 | ERROR | 任务ID、错误信息、堆栈 |

### 5.3 风控日志 (`risk.log`)

| 事件 | 级别 | 记录内容 |
|------|------|----------|
| 信号检查通过 | DEBUG | 信号详情、检查结果 |
| 信号被拦截 | WARNING | 拦截原因、信号详情 |
| 日亏损触发 | CRITICAL | 当前亏损、阈值、后续动作 |
| 持仓超限 | WARNING | 当前持仓数、上限 |

### 5.4 数据日志 (`data.log`)

| 事件 | 级别 | 记录内容 |
|------|------|----------|
| 数据库查询 | DEBUG | 查询类型、耗时、结果数 |
| AKShare 拉取 | INFO | 股票数、数据量、耗时 |
| 数据同步失败 | WARNING | 失败原因、降级处理 |
| 数据库异常 | ERROR | 异常类型、堆栈 |

---

## 6. 实现方案

### 6.1 改造 `utils/logger.py`

```python
"""
统一日志模块
- 支持控制台 + 文件双输出
- 支持 JSON 结构化日志
- 支持按模块分文件
- 支持日志轮转
"""

import json
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Optional

__all__ = ['get_logger', 'JsonFormatter']

LOG_DIR = './logs'
CONSOLE_FORMAT = '%(asctime)s [%(levelname)s] [%(module)s] %(message)s'
CONSOLE_DATE_FORMAT = '%Y-%m-%d %H:%M:%S'


class JsonFormatter(logging.Formatter):
    """JSON 结构化日志格式化器"""

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            'timestamp': datetime.fromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'module': record.module,
            'message': record.getMessage(),
        }
        # 附加业务数据（通过 extra={'data': {...}} 传入）
        extra_data = getattr(record, 'data', None)
        if extra_data is not None:
            log_data['data'] = extra_data
        # 异常信息
        if record.exc_info and record.exc_info[0]:
            log_data['exception'] = self.formatException(record.exc_info)
        # 堆栈信息
        if record.stack_info:
            log_data['stack_info'] = self.formatStack(record.stack_info)
        return json.dumps(log_data, ensure_ascii=False, default=str)


def get_logger(
    name: str,
    log_file: Optional[str] = None,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
) -> logging.Logger:
    """获取指定模块的 Logger。

    Args:
        name: Logger 名称，对应模块名（如 'live_trading'、'backtest'）
        log_file: 日志文件名（相对于 LOG_DIR），为 None 则不写文件
        console_level: 控制台输出级别，默认 INFO
        file_level: 文件输出级别，默认 DEBUG

    Returns:
        配置好的 logging.Logger 实例，含控制台 + 文件双输出
    """
    logger = logging.getLogger(name)

    if not logger.handlers:
        os.makedirs(LOG_DIR, exist_ok=True)

        # 控制台
        console = logging.StreamHandler()
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=CONSOLE_DATE_FORMAT))
        logger.addHandler(console)

        # 文件（带轮转，单文件 10MB，保留 5 个）
        if log_file:
            file_handler = RotatingFileHandler(
                os.path.join(LOG_DIR, log_file),
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding='utf-8',
            )
            file_handler.setLevel(file_level)
            file_handler.setFormatter(JsonFormatter())
            logger.addHandler(file_handler)

        logger.setLevel(logging.DEBUG)

    return logger
```

### 6.2 预定义 Logger 实例

```python
# 使用时直接导入
import logging
from utils.logger import get_logger

# 各模块 Logger
live_logger: logging.Logger = get_logger('live_trading', 'live_trading.log')
backtest_logger: logging.Logger = get_logger('backtest', 'backtest.log')
risk_logger: logging.Logger = get_logger('risk', 'risk.log')
data_logger: logging.Logger = get_logger('data', 'data.log')
```

### 6.3 日志调用示例

```python
# 信号生成
live_logger.info('信号生成', extra={
    'data': {
        'strategy': 'trend_following',
        'ts_code': '600519.SH',
        'signal': 'BUY',
        'weight': 0.15,
        'reason': '趋势跟随（得分0.20）',
        'price': 1262.98,
    }
})

# 风控拦截
risk_logger.warning('信号被拦截', extra={
    'data': {
        'ts_code': '600519.SH',
        'signal': 'BUY',
        'reason': '接近涨停(9.9%)，暂停买入',
    }
})

# 错误记录（exc_info=True 会自动将异常堆栈写入 JSON 的 exception 字段）
live_logger.error(f'策略执行异常: {e}', exc_info=True)
```

---

## 7. 实施步骤

| 步骤 | 内容 | 预计耗时 |
|------|------|----------|
| 1 | 改造 `utils/logger.py`，支持 JSON + 轮转 | 30 分钟 |
| 2 | 在 `live_server.py` 中替换 `print()` 为结构化日志 | 1 小时 |
| 3 | 在 `server.py` 中添加回测日志 | 30 分钟 |
| 4 | 在 `sigbus/filters.py` 中添加风控日志 | 30 分钟 |
| 5 | 在 `data/fetcher.py` 中添加数据日志 | 30 分钟 |
| 6 | 添加日志查看 API（`/api/logs`） | 30 分钟 |
| 7 | 添加日志查看页面（可选） | 1 小时 |

**总预计耗时：约 4 小时**

---

## 8. 日志查看方式

### 命令行查看

```bash
# 实时查看实盘日志
tail -f logs/live_trading.log

# 查看错误日志
grep '"level":"ERROR"' logs/*.log

# 查看某只股票的所有信号
grep '"ts_code":"600519"' logs/live_trading.log
```

### API 查看（可选）

```
GET /api/logs?module=live_trading&level=INFO&limit=100
GET /api/logs/search?keyword=600519
```

---

## 9. 注意事项

1. **性能影响** — 日志写入使用异步队列，避免阻塞主流程
2. **敏感信息** — 不记录 API Key、密码等敏感信息
3. **磁盘管理** — 自动轮转 + 保留最近 5 个备份，单模块最多 50MB
4. **编码兼容** — 统一使用 UTF-8 编码，支持中文
5. **生产环境** — 控制台级别设为 INFO，文件级别设为 DEBUG
6. **多进程安全** — `RotatingFileHandler` 在多进程（如 uvicorn workers）下轮转时不安全，可能丢失日志。若需多进程部署，建议改用 `QueueHandler` + `QueueListener` 模式，或使用 `loguru` 等支持多进程的日志库

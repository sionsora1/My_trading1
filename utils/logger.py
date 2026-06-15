"""
统一日志模块
- 支持控制台 + 文件双输出
- 支持 JSON 结构化日志（文件）和人类可读格式（控制台）
- 支持按模块分文件
- 支持日志轮转（RotatingFileHandler，单文件 10MB，保留 5 个备份）
- 每个 Logger 默认 propagate=False，避免日志污染其他日志文件

注意：RotatingFileHandler 在多进程（如 uvicorn workers > 1）下轮转时不安全。
若需多进程部署，建议改用 QueueHandler + QueueListener 模式。
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
        # 异常信息（exc_info=True 时自动捕获）
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
        配置好的 logging.Logger 实例，含控制台 + 文件双输出。
        同一个 name 只会创建一次 handler（幂等）。
    """
    logger = logging.getLogger(name)

    if not logger.handlers:
        os.makedirs(LOG_DIR, exist_ok=True)

        # 控制台 handler（人类可读格式）
        console = logging.StreamHandler()
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=CONSOLE_DATE_FORMAT))
        logger.addHandler(console)

        # 文件 handler（JSON 格式，带轮转）
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

        # 防止日志传播到 root logger，避免：
        # 1. 各模块日志污染 server.log
        # 2. 控制台重复输出
        logger.propagate = False

    return logger

"""
日志工具
"""

import logging
import os
from datetime import datetime


class Logger:
    """日志工具"""

    def __init__(self, name: str = 'quant_strategy', log_dir: str = './logs'):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG)

        if not self.logger.handlers:
            # 控制台输出
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_format = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                                                datefmt='%H:%M:%S')
            console_handler.setFormatter(console_format)
            self.logger.addHandler(console_handler)

            # 文件输出
            log_file = os.path.join(log_dir, f'{name}_{datetime.now().strftime("%Y%m%d")}.log')
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            file_format = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
            file_handler.setFormatter(file_format)
            self.logger.addHandler(file_handler)

    def info(self, msg: str):
        self.logger.info(msg)

    def debug(self, msg: str):
        self.logger.debug(msg)

    def warning(self, msg: str):
        self.logger.warning(msg)

    def error(self, msg: str):
        self.logger.error(msg)
# SPDX-License-Identifier: GPL-3.0-or-later
"""
日志初始化模块。

提供：
  - get_logger(name)   —— 获取带统一格式的 logger（子模块直接用它）
  - setup_logging()    —— 初始化：控制台 + 按天滚动的文件日志；重复调用幂等

统一格式：时间戳 | 级别 | 模块名 | 消息。
设计为显式调用 setup_logging() 一次（在应用/脚本入口），之后各模块用
get_logger(__name__) 获取即可。避免每次 import 自动配置导致重复 handler。
"""
import logging
import os
import sys
import io
from logging.handlers import TimedRotatingFileHandler

import liuhaitv.config as cfg

# 模块内 logger 命名空间：liuhaitv.*
_configured = False

# 统一格式
_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def get_logger(name: str) -> logging.Logger:
    """获取模块 logger（自动补齐 liuhaitv. 前缀避免与第三方日志混淆）。"""
    return logging.getLogger(name)


def setup_logging(level: int = logging.INFO) -> None:
    """
    初始化全局日志。重复调用不会重复添加 handler（幂等）。

    参数:
        level: 根 logger 级别，默认 INFO。
    """
    global _configured
    if _configured:
        return
    _configured = True

    try:
        cfg.ensure_dirs()
    except Exception:
        # 目录创建失败不应中断应用，日志退化为仅控制台
        pass

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()  # 清除默认/遗留 handler，避免重复

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # --- 控制台 handler ---
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # --- 文件 handler：按天滚动，保留 10 天 ---
    try:
        os.makedirs(cfg.LOGS_DIR, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            cfg.LOG_FILE,
            when="midnight",
            backupCount=10,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception as exc:  # 文件日志失败时退化为仅控制台
        logging.getLogger(__name__).warning(
            "文件日志初始化失败，仅使用控制台日志: %s", exc
        )

    logging.getLogger(__name__).info("日志系统已初始化，level=%s", logging.getLevelName(level))

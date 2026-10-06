# -*- coding: utf-8 -*-
"""日志器——本项目自建替身。

原版用 loguru 做彩色输出 + 文件轮转。我们只需要「同名 logger 能拿到、能打印」，
所以用标准库顶掉，省一个依赖（也避免和宿主项目的日志混在一起）。

公开 API 与原版一致：<平台>_logger，各带 .info/.debug/.warning/.error/.success
"""
import os
import sys
import time

# ★ 本项目改动：额外把每条日志追加到 SAU_LOG_FILE。
#   用途：SAU 遇到短信验证码弹窗时会写「等待验证码输入…可写入文件: xxx」，
#   我们的发布进程靠读这个文件把提示转达给 UI。
_LOG_FILE = os.environ.get("SAU_LOG_FILE") or ""


def set_log_file(path):
    global _LOG_FILE
    _LOG_FILE = str(path or "")


class _Logger:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def _emit(self, level, msg):
        line = "[sau %s %s] %s" % (time.strftime("%H:%M:%S"), level, str(msg))
        try:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        except Exception:
            pass
        if _LOG_FILE:
            try:
                with open(_LOG_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def trace(self, msg="", *a, **k):
        self._emit("TRACE", msg)

    def debug(self, msg="", *a, **k):
        self._emit("DEBUG", msg)

    def info(self, msg="", *a, **k):
        self._emit("INFO", msg)

    def success(self, msg="", *a, **k):
        self._emit("SUCCESS", msg)

    def warning(self, msg="", *a, **k):
        self._emit("WARN", msg)

    def error(self, msg="", *a, **k):
        self._emit("ERROR", msg)

    def exception(self, msg="", *a, **k):
        self._emit("ERROR", msg)

    def critical(self, msg="", *a, **k):
        self._emit("CRIT", msg)


douyin_logger = _Logger("douyin")
tencent_logger = _Logger("tencent")
kuaishou_logger = _Logger("kuaishou")
xiaohongshu_logger = _Logger("xiaohongshu")
weibo_logger = _Logger("weibo")
bilibili_logger = _Logger("bilibili")
baijiahao_logger = _Logger("baijiahao")
tiktok_logger = _Logger("tiktok")
alipay_logger = _Logger("alipay")
hupu_logger = _Logger("hupu")
youtube_logger = _Logger("youtube")

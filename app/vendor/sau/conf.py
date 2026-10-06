# -*- coding: utf-8 -*-
"""social-auto-upload 的配置——本项目自建版本。

原项目里 `conf.py` 是要用户从 `conf.example.py` 复制出来的，不随仓库分发。
这里给出我们集成所需的取值。

★ BASE_DIR 通过环境变量 `SAU_BASE_DIR` 覆盖：
  原项目把 cookies/、verify_code.txt、二维码图片都落在 BASE_DIR 下。
  我们的桥接层会把它指到「数据目录/sau」（即 app/data/sau），
  这样运行期产物不会写进源码树。
"""
import os
from pathlib import Path

_here = Path(__file__).parent.resolve()
BASE_DIR = Path(os.environ.get("SAU_BASE_DIR") or _here)

# 让 uploader/__init__.py 的 mkdir 在父目录还不存在时也成立
try:
    (BASE_DIR / "cookies").mkdir(parents=True, exist_ok=True)
except OSError:
    pass

XHS_SERVER = "http://127.0.0.1:11901"   # 仅 xhs 老流程用，本项目不使用
LOCAL_CHROME_PATH = ""                  # 留空＝用 patchright 自带的 chromium
LOCAL_CHROME_HEADLESS = True            # 默认无头；发布时由桥接层显式覆盖为有头
DEBUG_MODE = False
YT_PROXY = None                         # 仅 YouTube 用，本项目不支持

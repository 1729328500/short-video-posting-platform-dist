# -*- coding: utf-8 -*-
"""浏览器 channel 的选择 —— 跨平台部署的关键一处。

背景：本项目原本到处写 `channel="msedge"`（复用本机装的 Edge，省得再下一份浏览器）。
这在 Windows 上没问题，但**Linux 容器里没有 Edge**，会直接启动失败。
所以统一收口到这里：

    Windows  → msedge     （本机就有）
    Linux    → chromium   （playwright 自带那份，容器里唯一可用的）

需要强制指定时用环境变量 `VP_BROWSER_CHANNEL`：
    VP_BROWSER_CHANNEL=chrome     用系统 Chrome
    VP_BROWSER_CHANNEL=chromium   用 playwright 自带 chromium
    VP_BROWSER_CHANNEL=           空字符串＝用 playwright 默认（等同 chromium）

⚠️ 注意：本项目自己那条链路（登录窗 / 发布 / 抓取）用这个；
   vendored 的 social-auto-upload 用 `channel="chromium"`，那边本来就是跨平台的。
"""
import os
import sys


def channel():
    v = os.environ.get("VP_BROWSER_CHANNEL")
    if v is not None:
        return v.strip()
    return "msedge" if sys.platform == "win32" else "chromium"


def launch_kwargs(**extra):
    """方便直接展开：p.chromium.launch_persistent_context(**browser_channel.launch_kwargs(user_data_dir=..., ...))"""
    kw = {"channel": channel()}
    kw.update(extra)
    return kw


def auth_headless():
    """导出/导入登录态时，该不该用**无头**浏览器。

    ★ 为什么单独管这一件事（2026-09-28 实测）：
      抖音能识别无头浏览器 —— 这一点 Dockerfile 里早就写了
      （「实测抖音能识别无头浏览器，无头扫码登录走不通」，所以容器里让浏览器
      以有头模式跑在 Xvfb 上）。而 `sau_bridge.export_storage_state` /
      `import_storage_state` 里**写死了 headless=True** —— 等于：

        每次登录成功后，用无头浏览器开一次 profile 去灌 cookie；
        每次发布前，又用无头浏览器开一次 profile 去导出 cookie。

      实测那天的现象：8 个平台里**只有抖音掉登录**，而它恰好是唯一发过视频的；
      其余 6 个没发过的都活着。⇒ 「无头」是最可疑的那个变量，先把它去掉。

      ⚠️ 这是**基于证据的推断，不是已证实的因果** —— 抖音的风控是黑盒。
      容器里有 Xvfb，所以 Linux 上一律有头，成本为零；
      Windows 开发机保持无头，否则跑测试会弹一堆真窗口。

    用环境变量 `VP_AUTH_HEADLESS` 强制覆盖（1/0）。
    """
    v = os.environ.get("VP_AUTH_HEADLESS")
    if v is not None and v.strip() != "":
        # ★ 小写化再比（OCR 2026-09-28 指出）：原来只列了 "false"/"False"、
        #   漏了 "FALSE"/"No"/"OFF" —— 写错大小写会被当成「无头」，而这里的
        #   默认恰恰是想避免 Windows 上弹真窗口。统一 lower() 一次解决。
        return v.strip().lower() not in ("0", "false", "no", "off")
    return sys.platform == "win32"

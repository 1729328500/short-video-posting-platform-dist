# -*- coding: utf-8 -*-
"""把 social-auto-upload 的上传器接到本项目的发布链路上。

═══ 为什么需要这一层 ═══

两个世界对不上的地方只有一个：**登录态怎么存**。

    本项目    持久化 Edge profile（扫码登录，登录态落在 profile 目录里）
    SAU       storage_state JSON（Playwright 的 cookie 快照文件）

桥接做法：发布时用**我们自己的** profile 开一次浏览器，`context.storage_state()`
把 cookie 导出成它认识的 JSON，当作 `account_file` 喂给它的上传器。
⇒ 我们的扫码登录流程、账号页、状态灯**一行都不用改**。

═══ 其余约定 ═══

· 运行期产物（cookies/、verify_code.txt、二维码图）落在 `<数据目录>/sau`，
  通过环境变量 SAU_BASE_DIR 指过去，源码树保持干净。
· **短信验证码**：SAU 会在 BASE_DIR 下找 `verify_code.txt`。我们把它原样保留，
  并把 SAU 的日志转发到文件，好让 UI 能提示「平台要求验证码，请填入」。
· `uploader/`、`utils/`、`myUtils/` 是 vendored 的第三方代码（MIT，
  见 vendor/sau/LICENSE），除两处替身模块和一处解耦外未改动。
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import browser_channel  # noqa: E402
import browser_state  # noqa: E402

VENDOR_DIR = APP_DIR / "vendor" / "sau"

# 本项目平台 key → SAU 上传器
PLATFORMS = {
    "douyin":   {"module": "uploader.douyin_uploader.main",      "cls": "DouYinVideo",      "name": "抖音"},
    "channels": {"module": "uploader.tencent_uploader.main",     "cls": "TencentVideo",     "name": "视频号"},
    "kuaishou": {"module": "uploader.ks_uploader.main",          "cls": "KSVideo",          "name": "快手"},
    "xhs":      {"module": "uploader.xiaohongshu_uploader.main", "cls": "XiaoHongShuVideo", "name": "小红书"},
    "weibo":    {"module": "uploader.weibo_uploader.main",       "cls": "WeiBoVideo",       "name": "微博"},
}


def available():
    """本项目里能用 SAU 上传器的平台"""
    return sorted(PLATFORMS)


def supports(key):
    return key in PLATFORMS


def platform_name(key):
    return (PLATFORMS.get(key) or {}).get("name", key)


# 封面参数名**各平台不一样**：抖音没有 `thumbnail_path`，只有横版/竖版两个槽；
# 其余平台（视频号/快手/小红书/微博）都叫 `thumbnail_path`。
# 传错名字不是被忽略，而是 **TypeError** —— 所以必须按平台分派，不能一刀切。
THUMBNAIL_KWARG = {
    "douyin": "thumbnail_portrait_path",     # 本项目素材以竖版为主
    "channels": "thumbnail_path",
    "kuaishou": "thumbnail_path",
    "xhs": "thumbnail_path",
    "weibo": "thumbnail_path",
}


def thumbnail_kwarg(key):
    """该平台收封面的参数名叫什么（未知平台退回 thumbnail_path）"""
    return THUMBNAIL_KWARG.get(key, "thumbnail_path")


def parse_topics(raw):
    """'#测试 #自动化' / 'a,b' → ['测试','自动化']（SAU 的 tags 是纯词，不带 #）

    ★ 为什么把 `#` 也当分隔符（2026-09-28 实测踩过）：
      原来只按「空白 / 逗号 / 顿号」切，再用 `lstrip("#")` 去掉开头的 #。
      `lstrip` **只去左边**，于是微博那种 `#词#`（首尾都有 #）的格式会出问题：

          '#旧房翻新 #墙面翻新'    → ['旧房翻新', '墙面翻新']     ✅
          '#旧房翻新# #墙面翻新#'  → ['旧房翻新#', '墙面翻新#']   ❌ 标签里留了个 #
          '#旧房翻新#墙面翻新'     → ['旧房翻新#墙面翻新']        ❌ 两个词粘成一个

      而 AI 一键生成给**微博**的正是 `#词#` 格式（见 server.py 的 `_ai_one`：
      `fmt = "#词#" if k == "weibo" else "#词"`）—— 等于微博一发就会带上一堆
      像 `旧房翻新#` 这样的烂标签。
      把 `#` 一并当分隔符就三条全对，且对「不带 # 的纯词」没有任何影响。
    """
    out = []
    for t in re.split(r"[\s,，、#]+", str(raw or "")):
        t = t.strip()
        if t:
            out.append(t)
    return out


def _prepare_env(data_dir, log_file=""):
    """把 vendor 目录挂上 sys.path，并把 SAU 的运行期目录指到数据目录"""
    v = str(VENDOR_DIR)
    if v not in sys.path:
        sys.path.insert(0, v)
    sau_base = Path(data_dir) / "sau"
    sau_base.mkdir(parents=True, exist_ok=True)
    os.environ["SAU_BASE_DIR"] = str(sau_base)
    if log_file:
        os.environ["SAU_LOG_FILE"] = str(log_file)
    return sau_base


def export_storage_state(profile_dir, out_json):
    """把本项目的持久化 profile 导成 SAU 要的 storage_state JSON。

    ★ 这里用的是**本项目的** playwright + msedge —— 因为 profile 是 Edge 建的。
      导出出来的 cookie JSON 是引擎无关的，patchright 那边能直接用。
    """
    from playwright.sync_api import sync_playwright
    out_json = Path(out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        with browser_state.persistent_context(p.chromium,
            user_data_dir=str(profile_dir), channel=browser_channel.channel(),
            # ★ 不用无头：抖音能识别无头浏览器，而这里是**发布前**用真实登录态开一次
            #   profile —— 全程有头才和「人在用」的样子一致。见 auth_headless() 的说明。
            headless=browser_channel.auth_headless(),
            viewport={"width": 1280, "height": 820}) as ctx:
            ctx.storage_state(path=str(out_json), indexed_db=True)
    return out_json


def import_storage_state(profile_dir, state_json):
    """把 storage_state JSON 里的 cookie 灌进本项目的持久化 profile。

    ★ 用途：服务器/容器形态下扫码登录产出的是 storage_state（SAU 那套格式），
      而本项目自己的链路（登录窗/发布/抓取）认的是**持久化 profile**。
      这里做一次导入，两边登录态就一致了 —— 否则会出现「网页说登录成功了，
      但发布时说没登录」的错位（本项目踩过同类的）。
    返回导入的 cookie 条数。
    """
    state = json.loads(Path(state_json).read_text(encoding="utf-8"))
    cookies = state.get("cookies") or []
    if not cookies:
        return 0
    from playwright.sync_api import sync_playwright
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        with browser_state.persistent_context(p.chromium,
            user_data_dir=str(profile_dir), channel=browser_channel.channel(),
            # ★ 不用无头：这里是**登录成功后**紧接着用真实登录态开一次 profile，
            #   「扫码刚成功 → 马上一次无头访问」在平台眼里太像自动化了。
            #   见 auth_headless() 的说明。
            headless=browser_channel.auth_headless(),
            viewport={"width": 1280, "height": 820}) as ctx:
            ctx.add_cookies(cookies)
            browser_state.close(ctx, profile_dir, required=True)
    return len(cookies)


def _run_async(key, kwargs, timeout):
    """在独立线程里跑 asyncio 循环，避免和外层事件循环打架"""
    import concurrent.futures

    def _work():
        meta = PLATFORMS[key]
        mod = __import__(meta["module"], fromlist=["x"])
        cls = getattr(mod, meta["cls"])
        uploader = cls(**kwargs)
        asyncio.run(uploader.main())
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_work).result(timeout=timeout)


def publish(key, video, title, body, topics, profile_dir, data_dir,
            headless=False, publish_date=0, timeout=900, log_file="",
            desc=None, thumbnail=None):
    """调用 SAU 上传器发布一条视频。

    返回 {ok, note, error, verify_code_file}
      ok=False 时 error 里带原因；需要短信验证码时 note 会说明去哪写。
    """
    if key not in PLATFORMS:
        return {"ok": False, "error": "该平台没有 SAU 上传器：%s" % key}

    name = platform_name(key)
    sau_base = _prepare_env(data_dir, log_file)
    verify_file = sau_base / "verify_code.txt"

    # ① 导出 cookie
    state_file = sau_base / ("state_%s.json" % key)
    try:
        export_storage_state(profile_dir, state_file)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "导出登录态失败（profile 可能被占用）：%s" % str(e)[:160]}

    # ② 组装参数（照它 CLI 的用法）
    kwargs = {
        "title": title or "",
        "file_path": str(video),
        "tags": parse_topics(topics),
        "publish_date": publish_date,
        "account_file": str(state_file),
        "headless": bool(headless),
    }
    if desc is not None:
        kwargs["desc"] = desc
    else:
        kwargs["desc"] = body or ""

    # ④ 封面：用户在素材页上传/截取的那张，有就带上。
    # ★ 微博是**硬性要求** —— 没封面它会直接 `raise ValueError`，整条发布失败
    #   （实测 2026-09-28 job_23：`微博视频发布必须提供封面图（--thumbnail）`）。
    #   其它平台封面是可选的（不给就平台自己从视频里抽一帧），给了就用用户的。
    #   ⚠️ 参数名各平台不同：抖音是 thumbnail_landscape_path / thumbnail_portrait_path
    #      （没有单独的 thumbnail_path），其余平台都叫 thumbnail_path。
    #      传错名字会 TypeError，所以这里必须按平台分派。
    #   ⚠️ 微博还要求封面 < 5MB；超了它自己会抛，我们上传时放宽到 20MB，
    #      所以这里不预判、让上游报出来更清楚。
    if thumbnail:
        tp = Path(thumbnail)
        if tp.exists():
            kwargs[thumbnail_kwarg(key)] = str(tp)

    # ③ 跑
    try:
        _run_async(key, kwargs, timeout)
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:300]
        low = msg.lower()
        hint = ""
        if ("timeout" in low) or ("超时" in msg):
            hint = "（超时）"
        return {"ok": False,
                "error": "%s发布失败%s：%s" % (name, hint, msg),
                "verify_code_file": str(verify_file)}

    # ④ ★ 回灌：上传器跑完后会把它**刷新过的** cookie 写回 account_file
    #    （上游原话：`await context.storage_state(path=self.account_file)`，
    #      日志里那句「🥳 cookie 更新完毕」就是它）。
    #    而本项目的持久化 profile 是**登录那一刻**的快照 —— 灌回去之前是「有去无回」：
    #    profile 里的凭据永不刷新，平台侧却在不停轮换，越用越像僵尸客户端。
    #    `add_cookies` 按 (name, domain, path) 覆盖合并，不会删掉其它 cookie，安全。
    #    ⚠️ 失败不影响发布结果 —— 发布已经成功了，回灌只是锦上添花。
    back = 0
    try:
        back = import_storage_state(profile_dir, state_file)
    except Exception as e:  # noqa: BLE001
        print("[sau] 回灌 cookie 失败（不影响本次发布）: %s" % str(e)[:140])

    # ★ 2026-09-29 真机教训（用户现场核对发现）：这里的"成功"是**上传器自报**的 ——
    #   上游的判据是"URL 跳到了作品管理页"。实测抖音：11:18:23 点发布 → **11:18:24 就宣布成功**，
    #   而页面上提示"服务器问题"、创作中心里**也没有新视频**。
    #   ⇒ 我们**没有独立核验**，文案就必须照实说 —— 不能把"上传器说成功"写成"已发布"
    #     （这正是本项目最忌讳的「看起来成功」）。独立核验（去作品管理页查标题在不在）
    #     见 `10-弹窗收尾-实施计划.md` 的待办；在那之前，统计里的"成功"要按这句打折看。
    return {"ok": True, "note": "%s：上传器报成功（未独立核验）—— 建议到创作中心核对" % name,
            "verify_code_file": str(verify_file), "cookie_back": back}

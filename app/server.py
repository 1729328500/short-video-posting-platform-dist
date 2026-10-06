# -*- coding: utf-8 -*-
"""短视频发布工具 · 本地服务（仅 Python 标准库，无第三方依赖）

用法：
    python server.py [--port 8947] [--data-dir <目录>] [--no-browser]
说明：
    - 本地网页工具：浏览器打开 http://127.0.0.1:8947 即用，数据全在本机。
    - 数据目录默认 app/data（视频文件 + SQLite 数据库）。
"""
import argparse
import atexit
import concurrent.futures
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
UI_DIR = APP_DIR / "ui"
APP_VERSION = "1.11.0"
DESKTOP_MODE = False
DESKTOP_INSTANCE_ID = ""
DESKTOP_DATA_ID = ""


def display_ok():
    """虚拟显示（Xvfb）还活着吗？—— 扫码登录 / 发布 / 数据抓取全靠它。

    ★ 为什么要在 health 里暴露（2026-09-27 实测踩过，排查了很久）：
      Xvfb 因为上次退出残留的 /tmp/.X99-lock 起不来，而入口脚本当时没有失败分支，
      于是**容器 healthy、接口全 200，但所有浏览器功能静悄悄地全废了** ——
      用户只会看到「点了没反应」。宁可顶栏亮红灯，也不要这种「看起来正常」。

    判据：DISPLAY 指向 X 时，/tmp/.X11-unix/X<n> 这个 socket 在，
    且 /proc 里确实有 Xvfb 进程 —— 两个都满足才算活着。
    本机（Windows）跑服务时 DISPLAY 不是 `:n` 形式，直接算正常（那边没有 Xvfb）。
    """
    d = os.environ.get("DISPLAY", "")
    if not d.startswith(":"):
        return True
    n = d[1:].split(".")[0] or "0"
    if not os.path.exists("/tmp/.X11-unix/X%s" % n):
        return False
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % name) as f:
                    if f.read().strip() == "Xvfb":
                        return True
            except OSError:
                continue
    except OSError:
        return False
    return False


def novnc_port():
    """noVNC 的对外端口；没开这条路就返回 0。

    ★ 用途：前端拿它拼「打不开？直接操作浏览器」的链接（见 ui/app.js 的 NOVNC）。
      端口由容器的 NOVNC_PORT 环境变量给；本机直接跑时一般没有 → 返回 0 → 按钮不显示。
    ★ 这不是秘密：compose 里就是公开映射的端口，扫一下也知道。放在 /api/health
      （未登录可读）是为了让登录页之外的地方也能拿到，不影响安全边界。
    """
    if os.environ.get("NOVNC", "1") == "0":
        return 0
    try:
        return int(os.environ.get("NOVNC_PORT", "0") or 0)
    except ValueError:
        return 0

def _ensure_stdio():
    """pythonw（无窗口）运行时 stdout/stderr 为 None：重定向到日志文件，避免日志写入把服务弄崩"""
    if sys.stdout is None or sys.stderr is None:
        try:
            folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "shipin-fabu" / "data" / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            log = open(folder / "server.log", "a", encoding="utf-8", buffering=1)
        except OSError:
            log = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log


_ensure_stdio()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}

# ---------------- 工具函数 ----------------
_bad_chars = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_filename(name: str) -> str:
    name = os.path.basename(str(name).replace("\\", "/"))
    name = _bad_chars.sub("_", name).strip().strip(".")
    if not name:
        name = "video_" + time.strftime("%Y%m%d_%H%M%S") + ".mp4"
    return name


def csv_cell(v) -> str:
    """把单元格值转成 CSV 安全文本（含双引号包裹）。

    ★ 2026-10-06（安全审查 R6）：双引号包裹**不能**阻止电子表格把它当公式 ——
      标题/错误说明来自外部内容，以 = + - @ 或制表/回车开头时会被解析成公式
      （CSV 注入）。前缀一个单引号是电子表格公认的"按文本处理"写法。
    """
    s = "" if v is None else str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):
        s = "'" + s
    return '"%s"' % s.replace('"', '""')


def unique_name(folder: Path, name: str) -> str:
    stem, ext = os.path.splitext(name)
    cand, i = name, 1
    while (folder / cand).exists():
        cand = "%s(%d)%s" % (stem, i, ext)
        i += 1
    return cand


def _iter_boxes(buf, off, end):
    """遍历 MP4 box：yield (type, content_start, content_end)"""
    while off + 8 <= end:
        size = int.from_bytes(buf[off:off + 4], "big")
        typ = bytes(buf[off + 4:off + 8]).decode("latin-1", "ignore")
        hdr = 8
        if size == 1:
            if off + 16 > end:
                break
            size = int.from_bytes(buf[off + 8:off + 16], "big")
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            break
        yield typ, off + hdr, off + size
        off += size


def _parse_mvhd(d: bytes):
    if len(d) < 20:
        return None
    ver = d[0]
    if ver == 1:
        if len(d) < 32:
            return None
        ts = int.from_bytes(d[20:24], "big")
        dur = int.from_bytes(d[24:32], "big")
    else:
        ts = int.from_bytes(d[12:16], "big")
        dur = int.from_bytes(d[16:20], "big")
    return (dur / ts) if ts else None


def _parse_tkhd(d: bytes):
    if len(d) < 84:
        return None
    ver = d[0]
    if ver == 1:
        if len(d) < 92:
            return None
        w = int.from_bytes(d[84:88], "big") / 65536.0
        h = int.from_bytes(d[88:92], "big") / 65536.0
    else:
        w = int.from_bytes(d[76:80], "big") / 65536.0
        h = int.from_bytes(d[80:84], "big") / 65536.0
    if w < 1 or h < 1:
        return None
    return {"width": int(round(w)), "height": int(round(h))}


def probe_mp4(path: Path) -> dict:
    """读取 mp4/mov 时长与分辨率；失败返回 {}（无解码依赖）"""
    res = {}
    try:
        fsize = path.stat().st_size
        with open(path, "rb") as f:
            off = 0
            dur = None
            wh = None
            while off + 8 <= fsize:
                f.seek(off)
                head = f.read(16)
                if len(head) < 8:
                    break
                size = int.from_bytes(head[0:4], "big")
                typ = head[4:8].decode("latin-1", "ignore")
                hdr = 8
                if size == 1:
                    if len(head) < 16:
                        break
                    size = int.from_bytes(head[8:16], "big")
                    hdr = 16
                elif size == 0:
                    size = fsize - off
                if size < hdr:
                    break
                if typ == "moov" and (size - hdr) <= 64 * 1024 * 1024:
                    f.seek(off + hdr)
                    data = f.read(size - hdr)
                    for t, s, e in _iter_boxes(data, 0, len(data)):
                        if t == "mvhd":
                            v = _parse_mvhd(data[s:e])
                            if v is not None:
                                dur = v
                        elif t == "trak":
                            for t3, s3, e3 in _iter_boxes(data, s, e):
                                if t3 == "tkhd":
                                    v = _parse_tkhd(data[s3:e3])
                                    if v:
                                        if wh is None or v["width"] * v["height"] > wh[0] * wh[1]:
                                            wh = (v["width"], v["height"])
                off += size
            if dur is not None:
                res["duration"] = round(dur, 2)
            if wh:
                res["width"], res["height"] = wh
    except Exception:
        return {}
    return res


# ---------------- 平台规则 & 格式化 ----------------
def _load_rules():
    try:
        data = json.loads((APP_DIR / "platform_rules.json").read_text(encoding="utf-8"))
        return data.get("platforms", {})
    except Exception:
        return {}


RULES = _load_rules()


def fmt_sec(s):
    s = int(round(s))
    if s < 60:
        return "%d 秒" % s
    if s < 3600:
        return ("%d 分 %d 秒" % (s // 60, s % 60)) if s % 60 else ("%d 分" % (s // 60))
    return "%d 小时 %d 分" % (s // 3600, (s % 3600) // 60)


def fmt_mb(n):
    mb = n / 1024.0 / 1024.0
    return ("%.1f MB" % mb) if mb < 1024 else ("%.2f GB" % (mb / 1024.0))


# ---------------- 账号登录（浏览器会话） ----------------
# ★ 2026-09-29：平台入口表挪进 `platform_login`（登录态的唯一事实来源）——
#   "核验登录态"的工具必须与登录窗用**同一份入口 + 同一套判据**，
#   否则核出来的结论和登录窗会不一致（那就是新的"看起来对"）。
from platform_login import LOGIN_URLS  # noqa: E402
# 已接入真实数据抓取的平台（以 data_worker.py 为唯一事实来源，避免两处写岔）
try:
    import data_worker as _dw
    DATA_WORKER_SUPPORTED = tuple(_dw.SUPPORTED)
except Exception:  # noqa: BLE001
    DATA_WORKER_SUPPORTED = ()

# social-auto-upload 桥接（发布引擎 + 可交互扫码登录）。挂了也不能让服务起不来。
try:
    import sau_bridge
except Exception as _e:  # noqa: BLE001
    print("[warn] sau_bridge 导入失败，发布/扫码登录将不可用：%s" % str(_e)[:120])

    class _NoSau:
        @staticmethod
        def available():
            return []
    sau_bridge = _NoSau()

WORKERS = {}
LOCAL_PREV_STATE = {}
WORKER_LOCK = threading.Lock()
CURRENT_PORT = 0
MOCK_LOGIN_KEYS = set()


def worker_status_path(store, key):
    d = store.data_dir / "browsers"
    d.mkdir(parents=True, exist_ok=True)
    return d / ("%s.status.json" % key)


def worker_cmd_path(store, key):
    """给登录窗工作进程下指令的通道（confirm / close）"""
    d = store.data_dir / "browsers"
    d.mkdir(parents=True, exist_ok=True)
    return d / ("%s.cmd" % key)


def send_worker_cmd(store, key, cmd):
    try:
        worker_cmd_path(store, key).write_text(cmd, encoding="utf-8")
        return True
    except OSError:
        return False


def worker_alive(key):
    p = WORKERS.get(key)
    return bool(p) and p.poll() is None


_PROBE_LAST = {"at": 0.0}


def maybe_probe_accounts(store):
    """已登录但状态文件里还没有账号 ID 的平台 → 后台探一次。

    为什么要自动触发：账号 ID 是本功能上线后才开始记的，之前扫过码的账号没有。
    不补上的话「单账号日更上限」会退回按平台计（换账号不重置），用户会莫名被拦。
    节流：10 分钟内最多探一次，避免刷账号页时反复开浏览器。
    """
    now = time.time()
    if now - _PROBE_LAST.get("at", 0) < 600:
        return False
    need = []
    for key in ("douyin",):  # 目前仅抖音支持识别账号身份，其他平台由显式核验检查登录态
        st = read_worker_status(store, key)
        if st.get("state") == "on" and not st.get("account") and not st.get("account_checked"):
            need.append(key)
    if not need:
        return False
    _PROBE_LAST["at"] = now

    def _work():
        try:
            subprocess.run([sys.executable, str(APP_DIR / "tools" / "probe_accounts.py"),
                            "--data-dir", str(store.data_dir), "--key", "douyin"],
                           cwd=str(APP_DIR), timeout=300, capture_output=True)
        except Exception:
            pass

    threading.Thread(target=_work, daemon=True).start()
    return True


# ── 核验登录态（2026-09-29）─────────────────────────────────────────────
# 背景：账号页读的是**状态文件**（＝"上次操作的记录"），不是事实 —— 实测视频号显示已登录、
#       一发布却报 cookie 失效（交接文档 §3.2 / 坑 2）。用户原话："全是已登录，刷新后还是一样"。
# 做法：后台跑 `tools/probe_accounts.py --verify` —— 它用**和登录窗完全同一套判据**
#       （platform_login.classify）真开一次浏览器，并**据实写回**状态文件；
#       界面轮询 /api/accounts 即可看到状态被纠正。
VERIFY_STATE = {}      # key -> {"state": running/done/error, "at": "HH:MM:SS", "note": ...}
VERIFY_LOCK = threading.Lock()


def verify_done_note(ok, why):
    """核验结束时的界面文案。

    ★ 2026-09-30 实测踩过两处，都属「看起来成功」那一族：
      ① 成功分支原来直接拿**子进程输出的最后一行**当 note —— 那行是**某一个平台**
         的结果，结果 8 个平台的核验状态里挂着同一条 xigua 的行（误导）。
      ② 失败判定只信子进程返回码，而 probe_accounts 旧版无论失败多少平台都
         `return 0` ⇒ 全失败也记「完成」。工具现在会返回 2（见其 docstring），
         这里保持：失败就把原因原样说清楚（那行会点名失败的平台）。
    """
    if ok:
        return "核验完成，状态已按实写回"
    return why or "核验失败（看服务日志）"


def verify_login_state(store, keys):
    """后台核验登录态；返回 (ok, error)。

    ★ 三个护栏都是必需的（核验要真开浏览器，会和发布/登录**抢同一个 profile**）：
      ① 正在发布 → 拒绝（PUB_RUN_LOCK 被引擎持有着）；
      ② 该平台登录窗还开着 → 拒绝（同一个 profile 同时只允许一个实例）；
      ③ 已在核验中 → 拒绝（避免重复开浏览器）。
    """
    if PUB_RUN_LOCK.locked():
        return False, "正在发布中（会抢同一个浏览器 profile）—— 等发布跑完再核验"
    with VERIFY_LOCK:
        busy = [k for k in keys if (VERIFY_STATE.get(k) or {}).get("state") == "running"]
        if busy:
            return False, "这些平台正在核验中：%s" % "、".join(busy)
        alive = [k for k in keys if worker_alive(k)]
        if alive:
            return False, "这些平台的登录窗还开着（会抢 profile），先关掉：%s" % "、".join(alive)
        for k in keys:
            VERIFY_STATE[k] = {"state": "running", "at": time.strftime("%H:%M:%S"),
                               "note": "正在开浏览器核验…"}

    def _work():
        argv = [sys.executable, str(APP_DIR / "tools" / "probe_accounts.py"),
                "--data-dir", str(store.data_dir), "--verify"]
        if len(keys) == 1:
            argv += ["--key", keys[0]]
        ok = False
        why = ""
        try:
            r = subprocess.run(argv, cwd=str(APP_DIR), timeout=900, capture_output=True)
            ok = (r.returncode == 0)
            # ★ 把工具的输出**真的落到日志里**（OCR 复查指出）：第一版只 capture 不打印，
            #   而失败文案却让用户"看服务日志" —— 日志里什么都没有。
            out = ((r.stdout or b"") + (r.stderr or b"")).decode("utf-8", "ignore")
            lines = [ln for ln in out.splitlines() if ln.strip()]
            if lines:
                print("[verify] %s" % " | ".join(lines[-6:])[:600])
                why = lines[-1][:90]
        except Exception as e:       # noqa: BLE001
            ok = False
            why = "启动核验失败：%s" % str(e)[:80]
            print("[verify] %s" % why)
        with VERIFY_LOCK:
            for k in keys:
                VERIFY_STATE[k] = {"state": "done" if ok else "error",
                                   "at": time.strftime("%H:%M:%S"),
                                   "note": verify_done_note(ok, why)}

    threading.Thread(target=_work, daemon=True).start()
    return True, ""


def current_account(store, key):
    """当前登录的是哪个账号 → (账号ID, 账号名)。

    ★ 用途：「单账号日更上限」必须按**账号**算。旧实现按平台算，
      换个账号进来配额不重置，用户被锁住还找不到原因（实测踩过）。
      账号身份由登录窗在判定登录成功时探测写入状态文件。
    """
    try:
        st = read_worker_status(store, key)
        return str(st.get("account") or ""), str(st.get("account_name") or "")
    except Exception:
        return "", ""


def adapter_profile_key(key):
    """某些平台与另一个平台共用创作后台和登录态（实测西瓜视频 = 抖音后台），
    适配器里用 `profile_key` 声明。登录窗与发布都据此复用同一个 profile 目录，
    免得出现「西瓜单独登录了一份、发的时候却用抖音那份」这种错位。"""
    try:
        d = json.loads((APP_DIR / "publish_adapters.json").read_text(encoding="utf-8"))
        return (d.get("platforms", {}).get(key) or {}).get("profile_key") or key
    except Exception:
        return key


def browser_profile_dir(store, key):
    return store.data_dir / "browsers" / adapter_profile_key(key)


def shared_login_key(key):
    """该平台是否与别的平台**共用登录态**（西瓜视频 = 抖音创作者后台）。

    ★ 共用登录态的平台**不能有自己的登录窗** —— 实测踩过：西瓜的登录窗去开
      抖音的 profile，而抖音那个正被自己的窗口占着，Chromium 直接
      `Target page, context or browser has been closed` 启动失败。
      共用后台就该共用登录：西瓜跟随抖音的状态，登录也走抖音那一套。
    """
    pk = adapter_profile_key(key)
    return pk if pk != key else key


# ---------------- 可交互扫码登录（服务器/容器形态） ----------------
# 与 login_worker 的区别：那个在本机弹窗口给用户看；这个是「无窗口 + 文件协议」，
# 把当前该做什么写进 status.json，由网页呈现、再把用户指令写回 cmd 文件。
# 于是【登录全程都能在浏览器里完成】，容器里也能用。
QR_WORKERS = {}
QR_LOCK = threading.Lock()
QR_IMPORTED = set()          # 已把登录态导入 profile 的 key，避免重复导入
# key -> (status, note)：**开始扫码登录之前**的账号状态。
# ★ 为什么要有它：取消登录时不能把账号无条件写成 off —— 用户可能只是中止一次
#   「重新登录」，而他原本的登录态（profile 里的 cookie）压根没动。原来无条件写 off
#   等于「点一下取消就把登录态作废」，实测踩过（把已登录的抖音标成了未登录）。
#   取消时还原成开始前的状态。
QR_PREV_STATUS = {}


def qr_work_dir(store, key):
    d = store.data_dir / "qrlogin" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def qr_read_status(store, key):
    p = qr_work_dir(store, key) / "status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "off", "note": ""}


def qr_send_cmd(store, key, cmd):
    try:
        (qr_work_dir(store, key) / "cmd").write_text(cmd, encoding="utf-8")
        return True
    except OSError:
        return False


def qr_alive(key):
    pr = QR_WORKERS.get(key)
    return bool(pr) and pr.poll() is None


def qr_login_running(store, key):
    """这个平台的扫码登录进程**还在等**吗？

    ★ 2026-09-30 实测痛过：`start_qr_login` 一进门就 kill_worker 掉上一个进程 ——
      于是「登了没反应 → 再点一次登录」正好把**正在盯着扫码的那个进程**杀掉重开。
      实测一天里视频号被点了 10 次（抖音 10、快手 9）。而判登录要连续 2 轮
      （约 4 秒，见 STABLE_ROUNDS）都看到已登录才收尾 —— 被杀掉的那次永远等不到。
      ⇒ **重复点「登录」必须幂等**：已有登录窗在等就什么都不做。
    返回 True = 已有登录窗在等（opening/qr/verify_choice/sms_input 都算"在等"）。
    """
    proc = QR_WORKERS.get(key)
    if proc is None or proc.poll() is not None:
        return False
    st = qr_read_status(store, key)
    if not isinstance(st, dict):      # 状态文件被写坏时别炸，按"没在等"处理
        return False
    return st.get("state") in ("opening", "qr", "verify_choice", "sms_input")


def start_qr_login(store, key):
    """起一个可交互扫码登录进程。★ 一律有头 —— 实测抖音识别无头浏览器（扫码过不去）。

    ★ 2026-09-30：**幂等**（见 qr_login_running）—— 已经在等的登录窗不重开，
      也不再 kill 掉它。要重开请先点「取消登录」（那条路会明确清场）。
    """
    if qr_login_running(store, key):
        print("[qr] %s 已有登录窗在等 —— 不重开（幂等）" % key, flush=True)
        return {"key": key, "state": "already"}
    work = qr_work_dir(store, key)
    for f in ("status.json", "cmd", "qr.png", "state.json"):
        try:
            (work / f).unlink()
        except OSError:
            pass
    QR_IMPORTED.discard(key)
    state_out = work / "state.json"
    args = [sys.executable, str(APP_DIR / "login_qr_worker.py"),
            "--key", key, "--work", str(work),
            "--profile", str(browser_profile_dir(store, key)),
            "--state-out", str(state_out),
            "--headless", "0"]
    log_path = work / "worker.log"
    logf = open(log_path, "ab")
    with QR_LOCK:
        kill_worker(QR_WORKERS.get(key))   # 换新的之前，旧的连它起的浏览器一起收掉
        try:
            QR_WORKERS[key] = subprocess.Popen(args, cwd=str(APP_DIR),
                                               stdout=logf, stderr=subprocess.STDOUT,
                                               **worker_popen_kw())
        finally:
            logf.close()
    # 记下开工前的状态，取消/放弃时还原（见 QR_PREV_STATUS 的说明）
    try:
        prev = next((a for a in store.list_accounts() if a.get("key") == key), {})
        QR_PREV_STATUS[key] = (prev.get("status") or "off", prev.get("note") or "")
    except Exception:  # noqa: BLE001
        QR_PREV_STATUS[key] = ("off", "")
    store.set_account(key, "waiting", "扫码登录中…")
    # ★ 看门线程：进程自己跑完就收尾导入，不再依赖前端轮询（见 maybe_finish_qr_login）
    proc = QR_WORKERS.get(key)
    if proc is not None:
        try:
            threading.Thread(target=_qr_import_watcher, args=(store, key, proc),
                             daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
    return {"key": key, "state": "opening"}


def finish_qr_login(store, key):
    """登录成功后的收尾：把 storage_state 灌进本项目的持久化 profile。

    ★ 为什么必须做：可交互登录产出的是 storage_state（SAU 那套），
      而本项目自己的链路认的是持久化 profile。不导入就会出现
      「网页说登录成功、发布时说没登录」的错位。
    """
    work = qr_work_dir(store, key)
    st = qr_read_status(store, key)
    state_file = st.get("state_file") or str(work / "state.json")
    n = 0
    try:
        n = sau_bridge.import_storage_state(browser_profile_dir(store, key), state_file)
    except Exception as e:  # noqa: BLE001
        print("[qr] 导入登录态失败: %s" % str(e)[:140])
    acc = str(st.get("account") or "")
    acc_name = str(st.get("account_name") or "")
    # 顺手写进账号状态文件（与 login_worker 同口径），账号页与配额都认它
    write_worker_status(store, key, {"key": key, "state": "on", "url": st.get("url") or "",
                                     "note": "扫码登录成功", "account": acc, "account_name": acc_name,
                                     "detail": "已导入 %d 条 cookie 到持久化 profile" % n})
    store.set_account(key, "on", "扫码登录成功")
    return n


def maybe_finish_qr_login(store, key):
    """登录进程若已判定成功、而登录态还没导入过 → 导入一次。

    ★ 为什么要有这个「幂等的门」（2026-09-28 实测踩过）：
      导入原来只挂在 `/api/accounts/qr/status` 这个 GET 里，**靠前端轮询触发**。
      前端一旦在「状态变成 done 的那一刻」不在轮询 —— 用户点了「我已登录完成」、
      关了弹层、切走页面 —— 导入就永远不会发生。后果是：
        worker 报了登录成功、cookie 也导出到了 state.json，
        但持久化 profile 是空的 → 账号页显示绿色、发布时却说没登录。
      实测微博/B站就这样（cookie 抓到了 23/28 条，profile 目录压根没建）。
      现在三个入口都走它：状态轮询、手动确认、以及下面那个后台看门线程。
      靠 `QR_IMPORTED` 去重，重复调用安全。
    返回导入的 cookie 条数；不需要导入时返回 None。
    """
    try:
        st = qr_read_status(store, key)
    except Exception:  # noqa: BLE001
        return None
    if st.get("state") != "done":
        return None
    # ★ 「查重 + 占位」必须原子，否则三线程可以同时进来（2026-09-28 OCR 审查发现）：
    #   状态轮询 GET、手动确认 POST 各在自己的请求线程里，再加
    #   `_qr_import_watcher` 看门线程 —— ThreadingHTTPServer 下它们真会并发。
    #   原来写成「先 if key in QR_IMPORTED 再 add」，两个线程可以同时通过检查
    #   （都还没 add），于是 `finish_qr_login` 并发跑两次：对同一个持久化 profile
    #   目录并发 launch_persistent_context。而 Chromium 的 profile 同时只允许
    #   一个实例占用 —— 这条教训项目里已经写过（见 run_real_job 的注释），
    #   并发导入大概率有一边失败，甚至把 profile 写坏。
    #   （状态读取留在锁外：它只负责滤掉「还没到 done」，真正的去重靠锁内这次占位；
    #     把 done 判断也挪进锁内会让「不是 done 也占位」，语义就变了。）
    with QR_LOCK:
        if key in QR_IMPORTED:
            return None
        QR_IMPORTED.add(key)      # 先占位：即使下面抛异常，也不会反复重试
    try:
        return finish_qr_login(store, key)
    except Exception as e:  # noqa: BLE001
        print("[qr] 收尾导入失败(%s): %s" % (key, str(e)[:140]))
        return None


def _qr_import_watcher(store, key, proc):
    """登录进程自己跑完就收尾导入 —— **不再依赖前端轮询**。

    传进来的 proc 是本次登录专属的进程对象：只有它结束了、而且仍然是当前登记的
    那个 worker，才收尾。换新 worker（重新点登录）或用户取消时不导入。
    """
    try:
        while True:
            time.sleep(2)
            if proc.poll() is not None:
                break                      # 进程自己退了
            if QR_WORKERS.get(key) is not proc:
                return                     # 已被换新 worker 顶替 → 不归我管
    except Exception:  # noqa: BLE001
        return
    try:
        maybe_finish_qr_login(store, key)
    except Exception:  # noqa: BLE001
        pass


def cleanup_stale_workers(store, wait=12):
    """服务重启后收拾上一轮的遗留登录窗。

    ★ 为什么需要：Windows 上强杀服务进程（任务管理器 / taskkill / 计划任务重启）
      不会执行 atexit，`kill_all_workers` 跑不到 → 登录窗（Edge）活成孤儿，
      而 Chromium 持久化 profile 同时只允许一个实例占用 → 之后发布必然
      「profile in use」失败。实测踩过。
      这里给遗留的 key 发 close 指令（孤儿 worker 仍在轮询 cmd 文件，收得到），
      等它优雅关浏览器；等不到就把指令文件删掉，免得下一个新 worker 一启动就自杀。
    """
    pend = []
    for key in LOGIN_URLS:
        try:
            st = read_worker_status(store, key)
        except Exception:
            continue
        if st.get("state") in ("opening", "waiting", "unknown"):
            send_worker_cmd(store, key, "close")
            pend.append(key)
    if not pend:
        return 0
    per = max(1, wait // max(1, len(pend)))
    for key in pend:
        for _ in range(per * 4):
            try:
                if read_worker_status(store, key).get("state") in ("closed", "off"):
                    break
            except Exception:
                break
            time.sleep(0.25)
        cp = worker_cmd_path(store, key)
        try:
            if cp.exists():
                cp.unlink()
        except OSError:
            pass
    return len(pend)


def write_worker_status(store, key, obj):
    """原子写平台状态文件（临时文件 + os.replace）。

    ★ 为什么必须原子（2026-09-28 OCR 审查发现，当场坐实）：
      原来各处都是 `sp.write_text(...)` —— 那是 open-truncate-write，
      **读者可能读到写了一半的 JSON**。而 `refresh_accounts()` 每次
      `/api/accounts` 和 `/api/publish/create` 都会读状态文件，
      解析失败（见 read_worker_status）会兜底成 off，于是：
        已登录的账号被一次瞬时读错误标成「未登录」→ 用户点发布那一刻撞上，
        直接被拒「以下平台未登录」。
      `login_qr_worker.py` 里的 `w()` 早就用了「临时文件 + os.replace」，
      服务端这边没跟上。补上。
    """
    p = worker_status_path(store, key)
    o = dict(obj)
    o.setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
    tmp = Path(str(p) + ".tmp")
    try:
        tmp.write_text(json.dumps(o, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)              # 原子替换：读者要么看到旧内容，要么看到新内容
    except OSError:
        pass


# ★ 读不出来时的标记。见 read_worker_status 与 refresh_accounts 的 off 分支。
UNREADABLE = "_unreadable"


def read_worker_status(store, key):
    """读平台状态文件。

    ★ 两种「没有可用状态」必须分清（2026-09-28 OCR 审查发现）：
        · 文件**不存在** → 这平台从没登过，off 是事实，可以落库；
        · 文件在、但**解析不出来**（撞上写了一半）→ 这只是瞬时读错误，
          **不能**当成未登录落库，否则会把已登录的账号标成掉线。
      所以后者额外打一个 `_unreadable` 标记，由 refresh_accounts 决定跳过。
    """
    p = worker_status_path(store, key)
    if p.exists():
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(st, dict):
                return st
        except Exception:
            pass
        return {"key": key, "state": "off", UNREADABLE: True}
    return {"key": key, "state": "off"}


def start_worker(store, key, mock=False):
    # Serialize the complete start operation; repeated clicks reuse the same profile owner.
    with WORKER_LOCK:
        proc = WORKERS.get(key)
        if proc is not None and proc.poll() is None:
            return {"key": key, "state": "opening", "reused": True}
        prev = next((a for a in store.list_accounts() if a.get("key") == key), {})
        LOCAL_PREV_STATE[key] = dict(read_worker_status(store, key))
        QR_PREV_STATUS[key] = (prev.get("status") or "off", prev.get("note") or "")
        info = _start_local_worker(store, key, mock)
        store.set_account(key, "waiting", "正在打开登录窗…")
        return info


def _start_local_worker(store, key, mock=False):
    script = APP_DIR / "login_worker.py"
    prof = browser_profile_dir(store, key)   # 共用后台的平台会复用同一个 profile
    prof.mkdir(parents=True, exist_ok=True)
    status = worker_status_path(store, key)
    url = LOGIN_URLS.get(key, "")
    headless = False
    if mock:
        url = "http://127.0.0.1:%d/mock/login" % CURRENT_PORT
        headless = True
    args = [sys.executable, str(script), "--key", key, "--url", url,
            "--profile", str(prof), "--status", str(status),
            "--cmd", str(worker_cmd_path(store, key)),
            "--health", "http://127.0.0.1:%d/api/health" % CURRENT_PORT]
    if headless:
        args.append("--headless")
    log_path = store.data_dir / "browsers" / ("%s.log" % key)
    logf = open(log_path, "ab")
    try:
        WORKERS[key] = subprocess.Popen(args, cwd=str(APP_DIR), stdout=logf,
                                        stderr=subprocess.STDOUT, **worker_popen_kw())
    finally:
        logf.close()
    return {"key": key, "state": "opening"}


def stop_worker(store, key, mark_off=True):
    """关闭登录窗。

    mark_off=True  → 中止登录尝试时恢复原状态；已完成的登录保持有效
    mark_off=False → 为发布腾出 profile：只关窗口，保留登录态
                     （Chromium 持久化 profile 同时只能有一个实例，
                      发布要用同一个 profile，所以必须先把它让出来）
    """
    alive = worker_alive(key)
    if alive:
        # 让它自己优雅关浏览器（cookie 要在关闭时落盘），先发指令再等
        send_worker_cmd(store, key, "close")
        p = WORKERS.get(key)
        for _ in range(20):
            if p is None or p.poll() is not None:
                break
            time.sleep(0.5)
    with WORKER_LOCK:
        # 同上：这些 worker 也会起浏览器，只 terminate 会漏孤儿进程
        kill_worker(WORKERS.pop(key, None), grace=5)
    if mark_off:
        current = read_worker_status(store, key)
        original = LOCAL_PREV_STATE.pop(key, {})
        if not alive and current.get("state") == "on":
            QR_PREV_STATUS.pop(key, None)
            return {"key": key, "state": "on"}
        # ★ 关窗 ≠ 退出登录。用户关掉登录窗多半只是中止一次尝试，他 profile 里的
        #   cookie 根本没动；无条件写 off 等于「关个窗就把登录态作废」
        #   （跟 cancel 是同一个 bug，实测踩过）。有记录就还原，没有才退回 off。
        prev_status, prev_note = QR_PREV_STATUS.pop(key, ("off", "已关闭登录窗"))
        write_worker_status(store, key, dict(original, key=key, state=prev_status, url="", note=prev_note))
        return {"key": key, "state": prev_status}
    return {"key": key, "state": "on"}


def worker_popen_kw():
    """起 worker 时要多传的 Popen 参数。

    ★ start_new_session=True：让 worker **自成进程组**，kill_worker 才能一次
      killpg 把它和它起的浏览器全收掉。少了这一条，浏览器就会漏成孤儿（实测踩过）。
      POSIX 专属参数，Windows 上不传。
    """
    return {"start_new_session": True} if os.name == "posix" else {}


def kill_worker(pr, grace=8):
    """杀掉一个 worker 及其**整个进程组**（连带它起的浏览器）。

    ★ 为什么要连进程组一起杀（2026-09-27 实测踩过）：
      原来只 pr.terminate() —— 那只是给 python worker 发信号，而 chromium 是它的
      **子进程**：worker 一死，浏览器就成孤儿继续跑。每点一次【登录】就漏一个浏览器，
      实测攒到 30 个 chrome 进程，把 Xvfb 那个虚拟显示堆满，
      之后新浏览器启动开始报 `Target page, context or browser has been closed`
      —— 当时误判成平台风控，其实是自己漏出来的。
      现在 worker 用 start_new_session 自成进程组，这里一次 killpg 收干净。

    ⚠️ 自保：万一某个进程不是独立进程组（pgid 跟我们一样），killpg 会把**服务自己**
      一起带走。所以先比一下 pgid，不一样才用 killpg。
    """
    if pr is None or pr.poll() is not None:
        return
    try:
        if os.name == "posix" and os.getpgid(pr.pid) != os.getpgid(0):
            os.killpg(os.getpgid(pr.pid), signal.SIGTERM)
        else:
            pr.terminate()
    except Exception:  # noqa: BLE001
        try:
            pr.terminate()
        except Exception:
            pass
    try:
        pr.wait(timeout=grace)
    except Exception:  # noqa: BLE001
        try:
            if os.name == "posix" and os.getpgid(pr.pid) != os.getpgid(0):
                os.killpg(os.getpgid(pr.pid), signal.SIGKILL)
            else:
                pr.kill()
        except Exception:
            pass


def kill_all_workers():
    # 两类 worker 都走 kill_worker —— 它们**都会起浏览器**，只杀 python 进程会把
    # 浏览器漏成孤儿（见 kill_worker 的说明）。
    with WORKER_LOCK:
        for _key, p in list(WORKERS.items()):
            kill_worker(p, grace=3)
        WORKERS.clear()
    # 扫码登录 worker 也要一起收 —— 否则它们会活成孤儿占着工作目录里的日志文件
    # （实测踩过：测试跑完后 _tmp 删不掉，报 WinError 32）
    with QR_LOCK:
        for _key, pr in list(QR_WORKERS.items()):
            kill_worker(pr, grace=3)
        QR_WORKERS.clear()


atexit.register(kill_all_workers)


# ---------------- 配置（设置页） ----------------
DEFAULT_CONFIG = {
    # ★ 口令哈希/盐不在这里了 —— 2026-10-06（安全审查 R2）起住在 auth.json。
    #   留在 DEFAULT_CONFIG 里会被 norm_config 原样写回 config.json，
    #   等于口令字段又回到一个会损坏、会被整体覆盖的文件里。
    "发布间隔": [60, 180],
    "单账号日更上限": 2,
    # ★ 数据统计窗口（2026-09-30 口径变更）：数据页只统计「最近 N 天发布的那些作品」的
    #   数据之和；1 = 昨天 0 点到现在（默认）。窗口起点由 data_worker.win_start 统一算。
    "数据统计范围": 1,
    "播报时间": "20:00",
    "通知_本机": True,
    "通知_企微": True,
    "通知_飞书": True,
}


# config.json 的损坏状态 —— 供 /api/health 与设置页显示。
# ★ 为什么要有：损坏后的行为是"用默认值继续跑"，用户的设置等于被静默重置了。
#   不把它暴露出来，用户只会觉得"改了等于没改"（2026-10-06 安全审查 R2）。
CONFIG_STATE = {"损坏": False, "备份": ""}


def config_path(store):
    return store.data_dir / "config.json"


# ---------------- 口令存储（2026-10-06 安全审查 R2） ----------------
# ★ 为什么把口令从 config.json 拆出来单独一个文件：
#   原来 config.json 解析失败会被静默吞掉 → 退回默认配置（不含口令哈希）
#   → password_set() 报告"没设口令" → _gate 直接放行。**配置文件一坏，鉴权就没了**
#   （审查报告已动态复现：把配置改成不完整 JSON，无 Cookie 的请求从 401 变 200）。
#   拆开之后两条路各管各的：
#     · auth.json 不存在  = 首次启动（合法的本机免密码模式）
#     · auth.json 存在但读不懂 = 损坏 → 拒绝服务，绝不退回免密码
#   config.json 损坏则不再波及鉴权，应用照常用默认值跑（见 load_config）。

AUTH_DEFAULTS = {"口令哈希": "", "口令盐": "", "口令版本": 1, "更新时间": ""}


class AuthBroken(Exception):
    """auth.json 存在但读不出/解析不了 —— 必须失败关闭。"""


def auth_path(store):
    return store.data_dir / "auth.json"


def atomic_write_json(path, obj):
    """临时文件 + fsync + os.replace —— 与 launcher/common.py:atomic_json 同一套写法。

    ★ 为什么必须原子：直接 write_text 是 open-truncate-write，读取方可能撞上
      「文件已清空、新内容还没写完」的窗口 —— 审查报告正是在这个窗口里
      观察到无凭证请求被放行。原子替换让读者要么看到旧内容、要么看到新内容。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def load_auth(store):
    """读口令文件。

    文件不存在 → 首次启动，返回空口令（合法）。
    存在但解析失败 → 抛 AuthBroken（损坏，调用方必须失败关闭）。
    """
    p = auth_path(store)
    if not p.exists():
        return dict(AUTH_DEFAULTS)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                       # noqa: BLE001
        raise AuthBroken(str(e)[:120])
    if not isinstance(data, dict):
        raise AuthBroken("auth.json 不是对象")
    out = dict(AUTH_DEFAULTS)
    out.update(data)
    return out


def save_auth(store, auth):
    atomic_write_json(auth_path(store), auth)


def migrate_auth(store):
    """旧格式口令迁移 —— **只在启动时调用一次**。

    支持两种旧格式（都在 config.json 里）：
      · 哈希：`口令哈希` + `口令盐`  → 原样搬到 auth.json
      · 明文：`口令`                 → 加盐哈希后写 auth.json

    ★ 2026-10-06 复核发现后定的三条规矩（原来明文分支三条全违反）：
      1. **auth.json 已存在 → 它说了算**，绝不用遗留配置去覆盖它。
         原来明文分支不检查存在性，会把已有的 auth.json 覆盖掉并把版本**打回 1**
         —— 于是此前因改口令而失效的会话被重新激活。
      2. 认证写入**成功之后**才从 config.json 移除旧字段。
      3. 认证写入失败 → **抛出去（失败关闭）**，不能吞掉错误继续。
         原来 `except OSError: pass` 之后照样删掉明文 —— 应用直接变成没设口令。

    ★ 为什么必须从 load_config 里搬出来：那段是**每次读配置**都会跑的，
      不是一次性迁移。config.json 里的旧字段只要因为清理失败留在那儿，
      每次读都会重新覆盖一次 auth.json、每次把版本打回 1。
    """
    cp = config_path(store)
    if auth_path(store).exists() or not cp.exists():
        return                                   # 规矩 1：已有 auth.json 就不动认证
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
    except Exception:                            # noqa: BLE001
        return                                   # config 坏掉交给它自己的备份逻辑
    if not isinstance(data, dict):
        return

    h = str(data.get("口令哈希") or "")
    plain = str(data.get("口令") or "")
    if h:
        new_auth = {"口令哈希": h, "口令盐": str(data.get("口令盐") or ""),
                    "口令版本": 1, "更新时间": time.strftime("%Y-%m-%d %H:%M:%S")}
        drop = ("口令哈希", "口令盐")
    elif plain:
        new_auth = {"口令版本": 1, "更新时间": time.strftime("%Y-%m-%d %H:%M:%S")}
        set_password(new_auth, plain)
        drop = ("口令",)
    else:
        return

    save_auth(store, new_auth)                   # 规矩 3：失败会抛出去，由调用方失败关闭
    for k in drop:                               # 规矩 2：写成功之后才清理旧字段
        data.pop(k, None)
    atomic_write_json(cp, data)


def _hash_pw(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", str(pw).encode("utf-8"), bytes.fromhex(salt), 120000).hex()


def set_password(cfg, pw):
    pw = str(pw)[:64]
    if pw:
        salt = os.urandom(16).hex()
        cfg["口令盐"] = salt
        cfg["口令哈希"] = _hash_pw(pw, salt)
    else:
        cfg["口令盐"] = ""
        cfg["口令哈希"] = ""
    return cfg


def check_password(cfg, pw):
    h = cfg.get("口令哈希") or ""
    if not h:
        return True
    try:
        return _hash_pw(pw, cfg.get("口令盐") or "") == h
    except Exception:
        return False


# ---------------- 访问鉴权（v1.7） ----------------
# ★ 为什么必须有：本工具原本绑 127.0.0.1、没有鉴权 —— 那是本机桌面应用的前提。
#   一旦部署到服务器对外提供地址，**任何人**都能删素材、改配置、触发真实企微/飞书推送
#   （审查报告 2.5）。这里补上真正的鉴权。
#
# ★ 为什么用 Cookie 而不是自定义请求头：
#   封面、截图是浏览器用 <img src> 直接加载的，**发不了自定义头**，
#   只有 Cookie 会被自动带上。所以会话用 Cookie 承载。
# ★ SameSite=Lax 顺带解决跨站请求伪造（DNS-rebinding / CSRF）：
#   别站发起的 POST 不会带这个 Cookie。
SESSIONS = {}                    # token -> {"exp": 到期时间戳, "ver": 口令版本}
SESSION_TTL = 7 * 24 * 3600      # 7 天
SESSION_COOKIE = "vp_session"

# 服务是否只绑在本机（main() 里按实际的 --host 赋值）。
# ★ 为什么要有模块级变量：Handler 里拿不到 main() 的局部变量，
#   而"对外监听时不许运行中清空口令"这条判断要在请求处理里用（2026-10-06 安全审查 R3）。
SERVER_IS_LOOPBACK = True

# 不需要登录就能访问的路径（登录页要能加载、健康检查要能探活）
AUTH_EXEMPT = {"/", "/index.html", "/favicon.ico", "/api/health",
               "/api/auth/login", "/api/auth/status",
               # 登录助手（本机 exe）用它上传登录态 —— 它没有会话，靠请求体里的口令自证
               "/api/accounts/import"}

# 裸 body 接口（发原始字节，不是 JSON）—— 只有这两个不受「必须 application/json」的约束。
# 它们仍然走来源校验（见 _origin_ok）。
RAW_BODY_PATHS = {"/api/videos/upload", "/api/videos/cover"}

# ---- 请求体与上传限额（2026-10-06 安全审查 R5）----
# ★ 为什么必须有：上传原来是**持着 Store 全局锁**读整个网络请求体的，
#   且没有应用层读超时、没有体积上限 —— 一个慢连接就能让配置保存、素材删除
#   等所有用同一把锁的写操作一起卡住（写测试时实测到 15 秒仍未返回）。
MAX_UPLOAD_BYTES = int(os.environ.get("VP_MAX_UPLOAD_MB", "4096")) * 1024 * 1024
MAX_JSON_BYTES = int(os.environ.get("VP_MAX_JSON_MB", "4")) * 1024 * 1024
MAX_COVER_BYTES = 20 * 1024 * 1024          # 封面单独限额（原来只在路由里写死）
UPLOAD_TIMEOUT = float(os.environ.get("VP_UPLOAD_TIMEOUT", "300"))
JSON_TIMEOUT = 30.0
UPLOAD_SLOTS = threading.Semaphore(2)      # 同时进行的上传数


def new_session(ver=0):
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = {"exp": time.time() + SESSION_TTL, "ver": int(ver or 0)}
    # 顺手清理过期会话，避免内存里越攒越多
    now = time.time()
    for k in [k for k, v in SESSIONS.items() if v.get("exp", 0) < now]:
        SESSIONS.pop(k, None)
    return tok


def session_ok(tok, ver=0):
    """会话是否有效。

    ★ 2026-10-06（安全审查 R3）：除到期时间外还要比对口令版本 ——
      改口令必须能立刻踢掉所有旧会话，否则"改了密码"只是换了一把钥匙，
      早先发出去的会话（以及可能已泄露的 Cookie）照样能用满 7 天。
      （审查报告已复现：改口令后旧密码登录被拒，但两个原会话仍能访问业务接口。）
    """
    if not tok:
        return False
    rec = SESSIONS.get(tok)
    if not rec:
        return False
    if rec.get("exp", 0) < time.time():
        SESSIONS.pop(tok, None)
        return False
    return int(rec.get("ver", 0)) == int(ver or 0)


# 认证的读-改-写锁（2026-10-06 复核要求"序列化认证修改"）。
# ★ 只护读-改-写（改口令、迁移），不护 _gate 里的纯读 —— 那是每个请求都走的路径，
#   加锁等于把并发全串起来，而它只是读一个小文件。
AUTH_LOCK = threading.Lock()


def password_set(store):
    """是否设了口令。

    ★ 损坏时返回 True（失败关闭）—— 让调用方按「有口令但进不来」处理，
      而不是退回免密码模式。首启动（文件不存在）才返回 False。
    """
    try:
        return bool(load_auth(store).get("口令哈希") or "")
    except AuthBroken:
        return True


def public_config(cfg, auth=None):
    """给前端的配置视图 —— 永远不含口令字段。

    ★ 口令搬到 auth.json 后，`口令已设` 必须从 auth 读，不能再从 cfg 读
      （cfg 里已经没有这两个键了，从 cfg 读会永远显示「未设口令」）。
    """
    out = dict(cfg)
    out.pop("口令哈希", None)
    out.pop("口令盐", None)
    # ★ 明文「口令」也要挡掉：load_config 不再负责清它（旧字段可能还躺在 config.json 里，
    #   直到下一次保存才被 norm_config 丢掉）—— 不挡就会原样回给前端。
    out.pop("口令", None)
    out["口令已设"] = bool((auth or {}).get("口令哈希") or "")
    return out


def load_config(store):
    cfg = dict(DEFAULT_CONFIG)
    p = config_path(store)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
                CONFIG_STATE["损坏"] = False
        except Exception:                          # noqa: BLE001
            # ★ 2026-10-06（安全审查 R2）：不再静默吞掉。
            #   备份一份现场，并让 /api/health 与设置页显示「配置已损坏、已重置为默认值」。
            #   用户的设置确实丢了，必须让他看得见 —— 否则就是"改了等于没改"。
            #   （鉴权不受影响：口令已经在 auth.json，这条路径不再牵连免密码放行。）
            backup = p.with_name("config.json.broken-%s" % time.strftime("%Y%m%d%H%M%S"))
            try:
                p.replace(backup)
                CONFIG_STATE["损坏"] = True
                CONFIG_STATE["备份"] = str(backup)
            except OSError:
                pass
    # ★ 旧格式口令迁移**不在这里**（2026-10-06 复核发现后搬走）。
    #   这里会被每个请求调用；把它放这儿意味着"每次读配置都可能覆盖 auth.json"。
    #   现在统一在启动时由 migrate_auth() 做一次，见其 docstring。
    return cfg


def norm_config(cfg):
    out = dict(DEFAULT_CONFIG)
    try:
        arr = cfg.get("发布间隔", out["发布间隔"])
        a, b = int(arr[0]), int(arr[1])
        a = max(5, min(600, a))
        b = max(5, min(600, b))
        if a > b:
            a, b = b, a
        out["发布间隔"] = [a, b]
    except Exception:
        pass
    try:
        out["单账号日更上限"] = max(1, min(20, int(cfg.get("单账号日更上限", out["单账号日更上限"]))))
    except Exception:
        pass
    # ★ 2026-09-30（OCR 抓到的真问题）：norm_config 是**写死的透传表** —— 新键不加进来，
    #   用户保存时会被静默丢回默认值（设置页改了等于没改，--days 永远是 1）。
    #   ⚠️ 以后往 DEFAULT_CONFIG 加键，**必须同时在这里加一行**，否则就是"看起来能设"。
    try:
        out["数据统计范围"] = max(1, min(30, int(cfg.get("数据统计范围", out["数据统计范围"]))))
    except Exception:
        pass
    try:
        t = str(cfg.get("播报时间", out["播报时间"]))
        if re.match(r"^\d{1,2}:\d{2}$", t):
            out["播报时间"] = t
    except Exception:
        pass
    for k in ("通知_本机", "通知_企微", "通知_飞书"):
        out[k] = bool(cfg.get(k, out[k]))
    return out


def save_config(store, data):
    """保存配置。口令变更与版本递增在**同一次**认证写入里完成。

    ★ 2026-10-06 复核发现：原来是"先写新哈希、再单独 bump 版本"两次写入 ——
      第二次失败就留下「密码已经变了、旧会话却没被撤销」的状态。
      现在哈希/盐/版本一次落盘：要么全没变，要么全变。

    返回 (前端可见的配置, 认证快照, 是否动了口令)。
    """
    cfg = load_config(store)
    pw_changed = False
    with AUTH_LOCK:                      # 读-改-写整体串行，避免并发改口令互相覆盖
        try:
            auth = load_auth(store)
        except AuthBroken:
            auth = dict(AUTH_DEFAULTS)
        if isinstance(data, dict) and "口令" in data:
            newpw = str(data.get("口令") or "")
            set_password(auth, newpw)
            if newpw:
                # 与 set_password 同一次写入：版本号一起 +1
                auth["口令版本"] = int(auth.get("口令版本") or 1) + 1
            auth["更新时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_auth(store, auth)
            pw_changed = True
        if isinstance(data, dict):
            for k in DEFAULT_CONFIG:
                if k in data and k not in ("口令哈希", "口令盐"):
                    cfg[k] = data[k]
    cfg = norm_config(cfg)
    with store.lock:
        atomic_write_json(config_path(store), cfg)
    return public_config(cfg, auth), auth, pw_changed


def dir_stats(p):
    total = 0
    files = 0
    try:
        for f in Path(p).rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                    files += 1
                except OSError:
                    pass
    except OSError:
        pass
    return files, total


def harden_data_dir(store):
    """数据目录访问加固：仅当前 Windows 用户＋管理员可访问（icacls）"""
    if sys.platform != "win32":
        return False, "仅支持 Windows"
    user = os.environ.get("USERNAME") or ""
    if not user:
        return False, "获取当前用户名失败"
    exe = _resolve_bin("icacls", [r"C:\Windows\System32\icacls.exe"])
    args = [exe, str(store.data_dir), "/inheritance:r",
            "/grant:r", user + ":(OI)(CI)F",
            "/grant:r", "*S-1-5-32-544:(OI)(CI)F"]
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="gbk", errors="ignore", timeout=30)
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:120]
    if r.returncode == 0:
        return True, "已收紧：仅当前用户（%s）＋管理员可访问" % user
    return False, ((r.stdout or "") + (r.stderr or "")).strip()[:160] or "icacls 执行失败"


# ---------------- AI 文案（v1.1：一键生成各平台适配版） ----------------
AI_MOCK = False

PLAT_STYLE = {
    "douyin": "口语化、节奏快、开头要有钩子，标题短（15~25字为佳）",
    "channels": "亲切稳重、简洁干净",
    "kuaishou": "接地气、实在、像跟老乡唠嗑",
    "xhs": "种草风、多分段、可带 emoji，标题带吸引力",
    "weibo": "简短资讯感",
    "toutiao": "资讯风格、表达完整、照顾不了解背景的读者",
    "bilibili": "年轻化、口语、可以轻松一点，标题可用【】",
    "xigua": "通俗、面向大众、说人话",
}


def ai_config_path(store):
    return store.data_dir / "ai.json"


def load_ai_config(store):
    cfg = {"enabled": True, "api_base": "https://api.deepseek.com", "api_key": "", "model": "deepseek-chat"}
    p = ai_config_path(store)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def save_ai_config(store, dataset):
    """保存 AI 配置（开关 / 地址 / 密钥 / 模型）。

    ★ 为什么要有它（2026-09-28）：原来只有 `save_ai_enabled`，**只能存开关** ——
      `api_key`/`api_base`/`model` 三个字段界面上根本没地方填、接口也不收，
      要配只能 SSH 到 NAS 上手工编辑 data/ai.json。于是「⚡ 一键生成各平台适配版」
      在一台新部署的机器上永远是「未配置」，只能退化成基础版（8 平台同一份文案）。

    ★ 密钥只进不出：这里负责写；读取一律走 `/api/ai/status` 的 `configured` 布尔量，
      **从不把 api_key 回传给前端**（设置页的输入框因此永远是空的）。
      所以「留空」的语义是**不改动已有密钥**，而不是清空 —— 否则用户每次
      只改模型，都会把密钥一起抹掉。
    """
    cfg = load_ai_config(store)
    if "enabled" in dataset:
        cfg["enabled"] = bool(dataset.get("enabled"))
    for field in ("api_base", "model", "api_key"):
        if field in dataset:
            v = str(dataset.get(field) or "").strip()
            if v:                      # 留空＝不改动（见上面的说明）
                cfg[field] = v
    with store.lock:
        ai_config_path(store).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def _ai_one(cfg, k, title, body, topics):
    """单平台 AI 改写（供并发调用）；失败返回 None"""
    nm = RULES.get(k, {}).get("name", k)
    style = PLAT_STYLE.get(k, "")
    fmt = "#词#" if k == "weibo" else "#词"
    prompt = (
        "把下面这条「通用文案」改编成「%s」平台的版本（%s）。\n\n"
        "【通用文案】\n标题：%s\n正文：%s\n话题：%s\n\n"
        "【硬性规则】\n"
        "1. 只做改写，不编造原文之外的事实、数字、承诺。\n"
        "2. 正文适当分段，控制在 150 字以内，保持信息完整。\n"
        "3. 话题沿用原来的词，每个词加格式（%s），用空格分隔。\n"
        "4. 输出合法 JSON：{\"title\":\"\",\"body\":\"\",\"topics\":\"\"}。"
    ) % (nm, style, title[:200], body[:1500], topics[:300], fmt)
    payload = {
        "model": str(cfg.get("model") or "deepseek-chat"),
        "messages": [
            {"role": "system", "content": "你是短视频多平台运营文案专家，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 1.0,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        str(cfg.get("api_base") or "").rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + str(cfg.get("api_key") or ""),
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.loads(r.read().decode("utf-8"))
        data = json.loads(resp["choices"][0]["message"]["content"])
        if not isinstance(data, dict):
            return None
        return {"title": str(data.get("title") or "")[:200],
                "body": str(data.get("body") or "")[:4000],
                "topics": str(data.get("topics") or "")[:500]}
    except Exception:  # noqa: BLE001
        return None


def ai_generate(store, title, body, topics):
    """把通用文案改编为 8 平台适配版（并发）；返回 (copies, error)"""
    cfg = load_ai_config(store)
    if not cfg.get("enabled"):
        return None, "AI 生成已停用（可到设置页开启）"
    if AI_MOCK:
        copies = {}
        for k in LOGIN_URLS:
            nm = RULES.get(k, {}).get("name", k)
            copies[k] = {"title": "%s｜%s" % (title, nm),
                         "body": "%s\n（%s 适配版示例）" % (body, nm),
                         "topics": topics}
        return copies, None
    if not str(cfg.get("api_key") or "").strip():
        return None, "未配置 AI 密钥（app/data/ai.json）"
    copies = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_ai_one, cfg, k, title, body, topics): k for k in LOGIN_URLS}
        for fu in concurrent.futures.as_completed(futs):
            k = futs[fu]
            try:
                c = fu.result()
            except Exception:  # noqa: BLE001
                c = None
            if c:
                copies[k] = c
    good = len(copies)
    for k in LOGIN_URLS:
        if k not in copies:  # 个别失败 → 基础版补齐
            copies[k] = {"title": title, "body": body, "topics": topics}
    if good == 0:
        return None, "AI 调用失败（可稍后重试，或检查网络/key）"
    return copies, None


# ---------------- 发布引擎（队列 / 模拟与真实执行 / 完成通知） ----------------
PUB_RUN_LOCK = threading.Lock()
NOTIFY_DRY = False

# 单个发布任务的等待上限（秒）。要比 publish_worker / sau_bridge 内部的 900 秒宽 ——
# 抖音发布要人工在 noVNC 里填短信验证码，给足了再砍。见 run_real_job 里的说明。
PUB_WORKER_TIMEOUT = 1800

# 引擎是否正在「发布间隔」里等 —— 给界面显示倒计时用。
# {"until": 结束时刻(单调时钟), "seconds": 本次间隔总长, "at": 开始时刻}
# ★ 为什么要有它（2026-09-28 实测反馈）：引擎在每两个任务之间会按「发布间隔」
#   （默认 60~180 秒随机）睡一段，而界面上下一条任务只有「排队中」三个字 ——
#   用户分不清是**按设计在等**还是**卡死了**，实测就被当成 bug 报上来了。
#   把倒计时亮出来，等待与卡死就一眼可分。真正的卡死另有兜底：PUB_WORKER_TIMEOUT。
ENGINE_WAIT = {"until": 0.0, "seconds": 0.0, "at": ""}

# 用户点了「中止」的**任务号**。run_real_job 的等待循环每次醒来都查它，一看到就把
# 正在跑的发布进程连浏览器一起收掉、记成「已中止」。
# ★ 为什么要有它（2026-09-28 用户实测）：原来界面只有「取消未执行项」——
#   `cancel_batch` 的 SQL 是 `WHERE status='pending'`，**正在跑的那个根本不在范围内**。
#   账号被封（页面永远不变）或验证码迟迟不来时，任务会一直空转到 30 分钟超时，
#   用户**无路可走**。
# ★ 为什么按【任务】而不是【批次】（写第一版时差点踩进去）：批次内失败的任务
#   「重试」之后 batch 号**不变**；按批次立旗的话，一重试就会被自己的旗**当场秒杀**。
#   所以：立旗时记的是当时**正在跑的那个任务号**，且 run_real_job 开工第一件事
#   就是 `discard` 掉自己的旗 —— 重启同一条任务是干净的。
# 放内存里就够：进程一重启，那些 worker 本来也就没了。
PUB_CANCEL_JOBS = set()

# 浏览器/页面被人工关掉后，playwright 抛出来的话。SAU 的上传器**在重试循环里把这类
# 异常吞掉了**（只记一行「发布仍未完成(第N次)，异常: …」然后接着重试），所以光关窗
# 是停不下来的 —— 用户实测反馈「就算我关了窗口还是没有停止」。
# ⇒ 引擎侧读发布日志，看到这些字样就把这条任务收掉。
#   （和本项目「坑 2 / 坑 14」里那个 `Target page, context or browser has been closed`
#    是同一句话 —— 那次是浏览器漏成孤儿，这次是人工关窗，信号一样。）
BROWSER_GONE_MARKERS = (
    "Target page, context or browser has been closed",
    "Target closed",
    "browser has been closed",
    "Session closed",
)


def publish_log_path(status_file):
    """发布日志：job_<id>.json 旁边的 job_<id>.sau.log"""
    return Path(str(status_file).replace(".json", ".sau.log"))


def browser_closed_by_human(status_file):
    """人工把发布窗口关掉了吗？（读发布日志的尾部找信号）

    只读最后 4000 字符：日志会一直追加，全读会越来越慢；而"刚被关掉"的信号
    一定在尾部。
    """
    p = publish_log_path(status_file)
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            try:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4000))
            except OSError:
                f.seek(0)
            tail = f.read()
    except OSError:
        return False
    return any(m in tail for m in BROWSER_GONE_MARKERS)


def engine_wait_state():
    """引擎在等发布间隔吗？在等就返回剩余秒数，否则 None。

    ★ 用 `time.monotonic()` 而不是 `time.time()`（OCR 2026-09-28 指出）：
      这是**时长**语义。NAS 上 NTP 校时是常态，墙上时钟往回跳一下，倒计时就会
      显示成负数或突然变长。旧的 `time.sleep(iv)` 反而对时钟跳变免疫 ——
      加了这个状态之后不能把它弄丢。
    ★ 一次把三个字段抓进局部变量再算（OCR 同批指出）：引擎线程可能在两次读之间
      更新 ENGINE_WAIT，那样会把「A 窗口的剩余」和「B 窗口的总长」拼在一起。
      先快照成一个元组再算，就只会读到某一个窗口的完整数据。
    """
    snap = (ENGINE_WAIT.get("until"), ENGINE_WAIT.get("seconds"), ENGINE_WAIT.get("at"))
    until, secs, at = snap
    until = until or 0.0
    if until <= time.monotonic():
        return None
    return {"seconds": int(round(secs or 0)),
            "remaining": max(0, int(round(until - time.monotonic()))),
            "since": at or ""}


def _eng_log(store, msg):
    try:
        with (store.data_dir / "engine.log").open("a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass
MOCK_STEPS = ["打开创作页", "上传视频", "填写文案", "提交发布"]
WECOM_JS = "C:/Users/PC/.workbuddy/binaries/node/cli-connector-packages/node_modules/@wecom/cli/bin/wecom.js"
WECOM_TARGET = "woPzl7UQAAvuw1KkYr1l4RuU3XgWxM7A"
LARK_TARGET = "ou_5a0b7f061d23efd2f1bb34f0de7c4a35"


def _resolve_bin(name, fallbacks):
    p = shutil.which(name)
    if p:
        return p
    for f in fallbacks:
        if os.path.exists(f):
            return f
    return name


NODE_BIN = _resolve_bin("node", [r"C:\Users\PC\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"])
LARK_BIN = _resolve_bin("lark-cli", [r"C:\Users\PC\.workbuddy\binaries\node\cli-connector-packages\lark-cli.cmd"])


def run_mock_job(store, job):
    """模拟执行：**不碰浏览器、不真发**，只为演示流程。

    ★ 2026-09-30 实测事故：发布页的「模拟执行」复选框原来**默认勾选**，页面一刷新
      就回到勾选态 —— 用户没注意，整批发布全走了这里，5 秒一个"成功"，
      平台上什么都没有，而记录里写的是「已发布 ✓」，和真发一模一样。
      两头都改了：① 复选框默认不再勾（ui/index.html）；② 这里如实写
      「模拟完成（未真实发布）」，记录页还会打「模拟」徽章 —— 谁也别想再把模拟
      当成真发（本项目最忌的"看起来成功"，这次是被自己的默认值咬的）。
    """
    for st in MOCK_STEPS:
        store.update_job(job["id"], step=st)
        time.sleep(1.1)
    title = job.get("title") or ""
    if "[fail]" in title and (job.get("retries") or 0) == 0:
        store.update_job(job["id"], status="fail", step="失败", error="模拟失败：平台提示需要人工验证")
    else:
        store.update_job(job["id"], status="success", step="模拟完成（未真实发布）")


def wait_worker(p, deadline, on_tick=None, poll=2.0, should_stop=None):
    """等 worker 退出。返回它为什么结束：

        "done"    —— worker 自己退了
        "timeout" —— 到 deadline 还没退
        "stopped" —— should_stop() 说了停（用户点了「中止」）

    ★ 为什么单独抽出来（2026-09-28）：超时是「防批次永久卡死」的兜底，
      逻辑虽短，但夹在 run_real_job 里就没法单测 —— 只能干瞪着眼看常量。
      抽成函数后可以拿一个真会睡死的小进程来验它确实会超时返回 False。
    ★ 为什么返回的是原因而不是布尔（2026-09-28 晚）：加了「主动中止」之后，
      「超时」和「用户点了中止」要给出不同的文案与状态 —— 一个记 fail、
      一个记 canceled。挤在一个布尔里就分不出来了。
    """
    while p.poll() is None:
        if on_tick:
            try:
                on_tick()
            except Exception:  # noqa: BLE001
                pass
        if should_stop and should_stop():
            return "stopped"
        if time.monotonic() > deadline:      # deadline 必须是 monotonic 的
            return "timeout"
        time.sleep(poll)
    return "done"


def _worker_status_snapshot(status_file):
    """读 worker 状态文件 → `(state, note)`；不是终态或读不出来就是 `("", "")`。

    ★ 为什么要有（2026-09-29 真机：用户在收尾那一刻点「中止」）：
      worker 其实已把发布做完并写下 success，引擎却直接记 canceled ⇒ **记录说谎**：
      平台侧真发出去了、工具显示"已中止"；而「单账号日更上限」只统计
      success/manual/pending/running —— canceled **不计数** ⇒ 配额被少算。
    ★ 为什么一次读完（2026-09-29 OCR 复查指出）：早先拆成 `_worker_terminal_state` +
      `_worker_note` 两个函数，各读一遍同一个文件 —— 多余 IO，而且两次读之间文件可能被写
      （TOCTOU），会出现"state 是 success、note 是上一轮"这种对不上的组合。
    """
    try:
        d = json.loads(Path(status_file).read_text(encoding="utf-8"))
    except Exception:            # noqa: BLE001
        return "", ""
    if not isinstance(d, dict):
        return "", ""
    st = str(d.get("state") or "")
    return (st if st in ("success", "fail", "manual") else ""), str(d.get("note") or "")


def run_real_job(store, job):
    key = job["platform"]
    # ★ 开工先撤掉自己的「中止旗」：同一条任务被重试时，旗可能还留在上一轮
    #   （用户点了中止 → 任务变 canceled → 再点重试）。不撤就会被自己的旧旗
    #   当场秒杀，看起来像「重试按钮不管用」。见 PUB_CANCEL_JOBS 的说明。
    PUB_CANCEL_JOBS.discard(job["id"])
    script = APP_DIR / "publish_worker.py"
    status_file = store.data_dir / "publish" / ("job_%d.json" % job["id"])
    status_file.parent.mkdir(parents=True, exist_ok=True)
    row = store.get(job["video_id"]) if job.get("video_id") else None
    if not row:
        store.update_job(job["id"], status="fail", step="失败", error="素材不存在")
        return
    # ★ 发布前先把登录窗关掉，把 Chromium 持久化 profile 让出来。
    #   同一个 profile 同时只允许一个实例占用；而「账号已登录」恰恰意味着
    #   登录窗可能还开着 —— 不关就会 profile in use，发布必然失败。
    #   mark_off=False：只是关窗口腾地方，不能把登录态抹掉。
    #   共用后台的平台（西瓜=抖音）要连它父平台的登录窗一起关，因为占着的是同一个 profile。
    prof_key = adapter_profile_key(key)
    for k in dict.fromkeys([key, prof_key]):
        if worker_alive(k):
            store.update_job(job["id"], step="关闭登录窗、释放浏览器…")
            stop_worker(store, k, mark_off=False)
    # 把这条发布记到账号名下 —— 「单账号日更上限」按账号算，靠的是这个字段
    acc, acc_name = current_account(store, key)
    if acc:
        try:
            with store.lock:
                with sqlite3.connect(store.db_path) as c:
                    c.execute("UPDATE publishes SET account=?, account_name=? WHERE id=?",
                              (acc, acc_name, job["id"]))
        except Exception:
            pass
    args = [sys.executable, str(script), "--key", key, "--url", LOGIN_URLS.get(key, ""),
            "--profile", str(store.data_dir / "browsers" / prof_key),
            "--video", str(store.videos_dir / row["stored"]),
            "--title", job.get("title") or "", "--body", job.get("body") or "",
            "--topics", job.get("topics") or "", "--status", str(status_file),
            # ★ 把用户在素材页设的封面带上（2026-09-28）：微博**硬性要求**封面，
            #   不给就必失败；其它平台给了就用用户的、不给就平台自己抽帧。
            #   没设封面时传空串，各平台上游的封面步骤会自行跳过。
            "--thumbnail", str(store.covers_dir / row["cover"]) if row.get("cover") else "",
            # ★ 2026-09-29 用户决定：**一律自动点发布**，不再"停在提交前"。
            #   背景：适配器路径（头条/B站/西瓜）原先从不传 --submit → 永远停手；
            #   而 SAU 那 5 个平台（抖音/视频号/快手/小红书/微博）本来就是上传器直接发 ——
            #   于是"有的自动发、有的等人工"，口径不统一。现在统一成自动。
            #   ⚠️ 代价：这条安全网没了。要手工停手仍可以：手工跑
            #      `python publish_worker.py --key <平台> ...`（不带 --submit 即停手模式）。
            "--submit"]
    # ★ worker_popen_kw()：让发布进程也**自成进程组**，超时/收尾时才能一次 killpg
    #   把它和它起的浏览器一起收掉。原来这里是裸 Popen —— kill_worker 的自保判断
    #   （pgid 跟服务自己一样就退化成 terminate）会让浏览器漏成孤儿，
    #   正是坑 2 那一课，只是换到了发布这条路上，实测排查时发现。
    # ★ 先清掉上一轮的残留（OCR 2026-09-28 指出）：状态文件按**任务 id** 命名，
    #   同一条任务重试时路径相同。上一次（比如超时强杀）可能留下了「running / 发布中…」
    #   的残渣，新 worker 启动前那一小段窗口里 _tick 会把它当成本次进度刷到界面上；
    #   更糟的是万一新 worker 启动即崩、还没来得及写，最终会读到**上一轮的结果** ——
    #   上次是 fail 就按 fail 记、note 还是旧的，排查时张冠李戴。
    try:
        status_file.unlink()
    except OSError:
        pass
    p = subprocess.Popen(args, cwd=str(APP_DIR), **worker_popen_kw())
    # ★ 等待必须有上限（2026-09-28）：这段 while 卡住时整个批次就不会再往下走，
    #   后面所有任务在界面上永远停在「排队中」——「第二个一直在排队」就是这个形态
    #   （另一次是正常的发布间隔，见 ENGINE_WAIT）。worker 内部各有 900 秒上限，
    #   正常情况下会自己退；但「正常情况下会退」和「保证一定退」是两回事 ——
    #   浏览器卡死、平台把页面挂住时 poll() 可以一直不为 None。
    #   上限给得比内部那 900 秒宽：抖音发布要人工在 noVNC 里填短信验证码，
    #   别让人还没填完就被砍掉。
    def _tick():
        st = json.loads(status_file.read_text(encoding="utf-8"))
        store.update_job(job["id"], step=st.get("note") or st.get("state") or "")

    # ★ 单调时钟（OCR 2026-09-28 指出）：这是「30 分钟必退」这个**保证**，
    #   而墙上时钟会被 NTP 校时往回拉 —— 拉回 5 分钟，这个上限就变成 35 分钟。
    #   要的是「一定退」，就不能依赖时钟不跳。
    deadline = time.monotonic() + PUB_WORKER_TIMEOUT

    def _why_stop():
        """该不该停这条任务？返回原因（空串＝继续跑）。

        ★ 两条停的理由分开记（2026-09-28 用户实测）：
          · cancel       —— 用户点了「中止」
          · browser_gone —— 用户把 noVNC 里那个发布窗口**关掉了**
            第二条是刚需：SAU 的上传器在重试循环里把「页面已关闭」的异常吞掉接着
            重试，光关窗根本停不下来，用户原话「就算我关了窗口还是没有停止」。
        """
        if job["id"] in PUB_CANCEL_JOBS:
            return "cancel"
        if browser_closed_by_human(status_file):
            return "browser_gone"
        return ""

    reason = wait_worker(p, deadline, on_tick=_tick, should_stop=lambda: bool(_why_stop()))
    if reason != "done":
        kill_worker(p)          # 连浏览器进程组一起收，别留孤儿
        why = _why_stop() or "cancel"
        # ★ 2026-09-29 真机（用户实测）：**在收尾那一刻点「中止」**时，worker 其实已经把
        #   发布做完了（状态文件里写着 success），而这里直接记 canceled ⇒ **记录说谎**：
        #   平台侧视频真发出去了，工具却显示"已中止"；更实际的影响是
        #   「单账号日更上限」只统计 success/manual/pending/running —— canceled **不计数** ⇒ 配额少算。
        #   ⇒ 先读一眼状态文件：worker 已经给出终态，就**尊重它的结论**。
        _early, _early_note = _worker_status_snapshot(status_file)
        if _early:
            _eng_log(store, "job #%d 被停时 worker 已给出终态(%s)，按它记" % (job["id"], _early))
            store.update_job(job["id"], status=_early, step="已收尾", error=_early_note[:200])
            return
        if reason == "stopped" and why == "browser_gone":
            _eng_log(store, "job #%d 发布窗口被关闭，收尾" % job["id"])
            store.update_job(job["id"], status="canceled", step="已中止",
                             error="发布窗口被关闭，本条已中止（其余任务不受影响）")
        elif reason == "stopped":
            _eng_log(store, "job #%d 被手动中止" % job["id"])
            store.update_job(job["id"], status="canceled", step="已中止",
                             error="已手动中止（正在跑的任务被强制结束）")
        else:
            _eng_log(store, "job #%d 超时（%d 分钟），强制结束"
                     % (job["id"], PUB_WORKER_TIMEOUT // 60))
            store.update_job(job["id"], status="fail", step="失败",
                             error="发布超时（超过 %d 分钟仍未结束），已强制中止"
                                   % (PUB_WORKER_TIMEOUT // 60))
        return
    try:
        st = json.loads(status_file.read_text(encoding="utf-8"))
    except Exception:
        st = None
    if not st:
        store.update_job(job["id"], status="fail", step="失败", error="工作进程异常退出")
        return
    state = st.get("state")
    # ★ 发布时发现未登录 → 顺手把账号状态改成「未登录」。
    #   否则账号页会一直显示已登录（它只知道「上次登录成功过」），用户点了发布才失败。
    if st.get("reason") == "not_logged_in":
        # ★ 两处都要写：只改数据库没用 —— /api/accounts 会调 refresh_accounts()，
        #   它从【账号状态文件】读回 "on" 把数据库覆盖回去（实测踩过）。
        write_worker_status(store, key, {"key": key, "state": "off", "url": "",
                                         "note": "登录态已失效，请重新扫码"})
        try:
            store.set_account(key, "off", "登录态已失效，请重新扫码")
        except Exception:
            pass
    if state == "success":
        store.update_job(job["id"], status="success", step="已发布 ✓")
    elif state == "manual":
        store.update_job(job["id"], status="manual", step="需人工确认", error=st.get("note", ""))
    else:
        store.update_job(job["id"], status="fail", step="失败", error=st.get("note", "未知错误"))


def _send_notify(store, content, cfg):
    try:
        with (store.data_dir / "notify.log").open("a", encoding="utf-8") as f:
            f.write("[%s] push:\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), content[:300]))
    except OSError:
        pass
    if NOTIFY_DRY:
        try:
            with (store.data_dir / "notify.log").open("a", encoding="utf-8") as f:
                f.write("[dry-run] 企微/飞书未真实发送\n")
        except OSError:
            pass
        return
    if cfg.get("通知_企微"):
        try:
            subprocess.Popen([NODE_BIN, WECOM_JS, "message", "aibot", "send", "--json",
                              json.dumps({"chat_id": WECOM_TARGET, "msg_type": "markdown", "content": content},
                                         ensure_ascii=False)], cwd=str(APP_DIR))
        except Exception:
            pass
    if cfg.get("通知_飞书"):
        try:
            subprocess.Popen([LARK_BIN, "im", "+messages-send", "--user-id", LARK_TARGET,
                              "--markdown", content], cwd=str(APP_DIR))
        except Exception:
            pass


FETCH_STATE = {"running": False, "note": ""}


def fetch_status_path(store):
    return store.data_dir / "fetch.status.json"


def write_fetch_status(store, obj):
    obj = dict(obj)
    obj["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    FETCH_STATE.update(obj)
    try:
        fetch_status_path(store).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def run_fetch(store):
    """真实抓取各平台数据。

    ★ 不再生成模拟数据。旧版用 md5(视频id|平台|日期) 造播放/点赞/评论，
      标了 source='mock' 但界面和推送都不显示 —— 等于拿编造数字充数。已废弃。
      没接入真实抓取的平台**如实返回「不支持」**，界面照实显示，宁可空着。
    """
    day = time.strftime("%Y-%m-%d")
    # ★ 统计窗口（2026-09-30 口径变更）：口径是"最近 N 天发布的那些作品"，N 来自设置页。
    #   兜底值取 DEFAULT_CONFIG 而**不是 _dw.DAYS_DEFAULT**（OCR 指出）：data_worker 是
    #   try-import 的可选依赖，导不进来时 `_dw` 根本没绑定 —— 那样这个 except 自己会 NameError。
    try:
        days = max(1, int(load_config(store).get("数据统计范围") or DEFAULT_CONFIG["数据统计范围"]))
    except Exception:                            # noqa: BLE001
        days = DEFAULT_CONFIG.get("数据统计范围", 1)
    # ★ 清掉历史遗留的模拟数据行。旧版 run_fetch 造过 source='mock' 的记录，
    #   它们会一直躺在库里、被播报当成真实数据推出去。真实抓取一启动就清干净。
    try:
        with store.lock:
            with sqlite3.connect(store.db_path) as c:
                c.execute("DELETE FROM metrics WHERE source IS NULL OR source='mock'")
    except Exception:
        pass
    results = {}
    for key in LOGIN_URLS:
        name = RULES.get(key, {}).get("name", key)
        if key not in DATA_WORKER_SUPPORTED:
            results[key] = {"ok": False, "not_supported": True,
                            "error": "该平台的真实抓取尚未接入"}
            continue
        st = read_worker_status(store, key)
        if st.get("state") != "on":
            results[key] = {"ok": False, "error": "未登录，请先扫码"}
            continue
        write_fetch_status(store, {"running": True, "note": "正在抓取 %s 的数据…" % name})
        status_file = store.data_dir / ("fetch_%s.json" % key)
        try:
            if status_file.exists():
                status_file.unlink()
        except OSError:
            pass
        try:
            subprocess.run([sys.executable, str(APP_DIR / "data_worker.py"), "--key", key,
                            "--profile", str(browser_profile_dir(store, key)),
                            "--status", str(status_file), "--days", str(days)],
                           cwd=str(APP_DIR), timeout=420, capture_output=True)
            res = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": str(e)[:140]}
        results[key] = res
        if res.get("ok"):
            wk = res.get("works") or {}
            ac = res.get("account") or {}
            store.upsert_metrics(day, key, None,
                                 wk.get("views", 0), wk.get("likes", 0), wk.get("comments", 0),
                                 "real:%s" % res.get("source", "api"),
                                 followers=ac.get("followers"), total_likes=ac.get("total_likes"),
                                 works_count=wk.get("count"), nick_name=ac.get("nick_name"))
    got = [k for k, v in results.items() if v.get("ok")]
    miss = [k for k, v in results.items() if v.get("not_supported")]
    write_fetch_status(store, {"running": False, "note": "抓取完成：成功 %d 个平台%s"
                               % (len(got), ("，%d 个平台未接入真实抓取" % len(miss)) if miss else "")})
    return results


def start_fetch(store):
    """抓取很慢（每个平台要开一次浏览器），所以放后台线程跑，接口立刻返回。"""
    if FETCH_STATE.get("running"):
        return False

    def _work():
        try:
            run_fetch(store)
        except Exception as e:  # noqa: BLE001
            write_fetch_status(store, {"running": False, "note": "抓取异常：%s" % str(e)[:140]})

    write_fetch_status(store, {"running": True, "note": "准备抓取…"})
    threading.Thread(target=_work, daemon=True).start()
    return True


def run_report(store):
    """数据播报：前一天发布过内容的平台，各自的**真实**数据 → 推送。

    ★ 两处口径变更（v1.5）：
      ① 数据来源：库里现在存的是各平台**账号作品合计**（真实抓取，source='real:*'），
         不再有逐条视频的数据 —— 所以播报口径相应说明为「作品合计」。
      ② **只推真实数据**：没有真实数据的平台如实写「未抓取/未接入」，
         绝不拿编造数字充当（旧版会把模拟数据当真实数据推出去）。
    """
    cfg = load_config(store)
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    rows = store._rows(
        "SELECT DISTINCT platform FROM publishes WHERE status='success' AND substr(created_at,1,10)=?",
        (yesterday,))
    if not rows:
        content = "**【短视频发布台】数据播报**\n\n前一天（%s）没有发布记录。" % yesterday
    else:
        lines = ["**【短视频发布台】数据播报（%s 发布过内容的平台）**" % yesterday, ""]
        miss = []
        for r in rows:
            k = r["platform"]
            nm = RULES.get(k, {}).get("name", k)
            m = store._rows(
                "SELECT * FROM metrics WHERE platform=? AND source LIKE 'real:%' "
                "ORDER BY day DESC, id DESC LIMIT 1", (k,))
            if not m:
                miss.append(nm)
                continue
            row = m[0]
            wk = ("（%d 条作品合计）" % row["works_count"]) if row.get("works_count") else ""
            lines.append("- %s%s：播放 %s · 点赞 %s · 评论 %s"
                         % (nm, wk, row["views"], row["likes"], row["comments"]))
            if row.get("total_likes"):
                lines.append("  - 账号累计：粉丝 %s · 总获赞 %s"
                             % (row.get("followers") or 0, row["total_likes"]))
        if miss:
            lines.append("")
            lines.append("- %s：未抓取到真实数据（已不再显示模拟数字）" % "、".join(miss))
        content = "\n".join(lines)
    _send_notify(store, content, cfg)
    return content


def report_scheduler(store):
    """每天到「播报时间」推送一次（当天错过的到点后补发一次）"""
    while True:
        time.sleep(15)
        try:
            cfg = load_config(store)
            target = cfg.get("播报时间") or "20:00"
            now = time.strftime("%H:%M")
            today = time.strftime("%Y-%m-%d")
            marker = store.data_dir / "report_last.txt"
            sent = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
            if now >= target and sent != today:
                marker.write_text(today, encoding="utf-8")
                run_report(store)
        except Exception:
            pass


def notify_batch(store, batch, rows):
    """发布批次完成通知：本机（UI 可见）＋企微＋飞书（按设置开关）"""
    cfg = load_config(store)
    okn = sum(1 for r in rows if r["status"] == "success")
    failn = sum(1 for r in rows if r["status"] == "fail")
    manualn = sum(1 for r in rows if r["status"] == "manual")
    lines = ["**【短视频发布台】发布完成**", "",
             "- 成功 %d · 失败 %d · 待人工 %d（批次 #%s）" % (okn, failn, manualn, batch)]
    for r in rows:
        flag = {"success": "✅", "fail": "❌", "manual": "⚠️", "canceled": "🚫"}.get(r["status"], "•")
        nm = RULES.get(r["platform"], {}).get("name", r["platform"])
        cell = "- %s %s" % (flag, nm)
        t = (r.get("title") or "").strip()
        if t:
            cell += "：「%s」" % t[:18]
        if r["status"] == "fail" and r.get("error"):
            cell += "（%s）" % r["error"][:40]
        lines.append(cell)
    content = "\n".join(lines)
    try:
        with (store.data_dir / "notify.log").open("a", encoding="utf-8") as f:
            f.write("[%s] batch=%s ok=%d fail=%d manual=%d\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), batch, okn, failn, manualn))
    except OSError:
        pass
    _send_notify(store, content, cfg)


def publish_engine(store):
    while True:
        time.sleep(1.5)
        try:
            with PUB_RUN_LOCK:
                job = store.next_publish_job()
                if not job:
                    continue
                store.update_job(job["id"], status="running", step="准备中")
            _eng_log(store, "pick job #%d [%s] batch=%d" % (job["id"], job["platform"], job["batch"]))
            try:
                if job.get("mock"):
                    run_mock_job(store, job)
                else:
                    run_real_job(store, job)
            except Exception as e:  # noqa: BLE001
                store.update_job(job["id"], status="fail", step="失败", error=str(e)[:150])
            try:
                st_row = store._rows("SELECT status FROM publishes WHERE id=?", (job["id"],))
                final_status = st_row[0]["status"] if st_row else "?"
                _eng_log(store, "finish job #%d -> %s" % (job["id"], final_status))
                if final_status == "fail":
                    if store.auto_stop_platform(job["batch"], job["platform"]):
                        _eng_log(store, "auto-stop %s in batch=%d" % (job["platform"], job["batch"]))
            except Exception:
                pass
            try:
                done, rows = store.batch_done(job["batch"])
                if done and not store.batch_notified(job["batch"]):
                    store.mark_batch_notified(job["batch"])
                    notify_batch(store, job["batch"], rows)
            except Exception:
                pass
            # ★ 队列里没有下一条任务了就别睡（2026-09-28）：原来每条任务跑完都睡满
            #   间隔，包括**最后一条** —— 明明没活了还睡 1~3 分钟，界面上白挂着
            #   「等待发布间隔」，紧接着再来一批也要跟着等。空转没有意义。
            if not store.next_publish_job():
                continue
            cfg = load_config(store)
            a, b = cfg.get("发布间隔") or [60, 180]
            iv = random.uniform(max(1, a), max(2, b))
            _eng_log(store, "interval sleep %.1fs" % iv)
            # ★ 小步睡而不是一觉睡到底：① 界面能拿到准确剩余秒数；
            #   ② 将来要「跳过等待/立即继续」时不用另加重启逻辑。
            #   窗口期内把状态亮出来，界面才不会把「按设计等待」当成卡死。
            end = time.monotonic() + iv      # 时长语义 → 单调时钟（墙上时钟会被 NTP 拉动）
            ENGINE_WAIT.update(until=end, seconds=iv, at=time.strftime("%Y-%m-%d %H:%M:%S"))
            try:
                while time.monotonic() < end:
                    time.sleep(0.5)
            finally:
                ENGINE_WAIT["until"] = 0.0
        except Exception:
            pass


# ---------------- 数据层 ----------------
class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.videos_dir = self.data_dir / "videos"
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self.covers_dir = self.data_dir / "covers"
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "app.db"
        self.lock = threading.Lock()
        # ★ 清扫上次中断留下的上传临时文件（R5）：进程被杀 / 断电时 finally 跑不到，
        #   不扫的话它们会一直躺在素材目录里（名字以 . 开头，用户在界面上看不见）。
        for stale in list(self.videos_dir.glob(".*.part")) + list(self.covers_dir.glob(".*.part")):
            try:
                stale.unlink()
            except OSError:
                pass
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS videos(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    stored TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    duration REAL,
                    width INTEGER,
                    height INTEGER,
                    added_at TEXT NOT NULL,
                    cover TEXT)"""
            )
            cols = [row[1] for row in c.execute("PRAGMA table_info(videos)").fetchall()]
            if "cover" not in cols:
                c.execute("ALTER TABLE videos ADD COLUMN cover TEXT")
            c.execute(
                """CREATE TABLE IF NOT EXISTS drafts(
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL)"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS accounts(
                    key TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'off',
                    note TEXT DEFAULT '',
                    updated_at TEXT NOT NULL)"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS publishes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch INTEGER NOT NULL,
                    video_id INTEGER,
                    platform TEXT NOT NULL,
                    title TEXT DEFAULT '',
                    body TEXT DEFAULT '',
                    topics TEXT DEFAULT '',
                    mode TEXT DEFAULT 'now',
                    scheduled_at TEXT DEFAULT '',
                    status TEXT DEFAULT 'pending',
                    step TEXT DEFAULT '',
                    error TEXT DEFAULT '',
                    retries INTEGER DEFAULT 0,
                    mock INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL)"""
            )
            pcols = [row[1] for row in c.execute("PRAGMA table_info(publishes)").fetchall()]
            # v1.6：记录这条发布属于哪个账号 —— 「单账号日更上限」要按账号算，
            #       旧实现按平台算，换账号也不重置（实测踩过）。
            if "account" not in pcols:
                c.execute("ALTER TABLE publishes ADD COLUMN account TEXT")
            if "account_name" not in pcols:
                c.execute("ALTER TABLE publishes ADD COLUMN account_name TEXT")
            c.execute(
                """CREATE TABLE IF NOT EXISTS notified_batches(
                    batch INTEGER PRIMARY KEY)"""
            )
            c.execute(
                """CREATE TABLE IF NOT EXISTS metrics(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    day TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    video_id INTEGER,
                    views INTEGER DEFAULT 0,
                    likes INTEGER DEFAULT 0,
                    comments INTEGER DEFAULT 0,
                    source TEXT DEFAULT 'mock',
                    updated_at TEXT NOT NULL)"""
            )
            mcols = [row[1] for row in c.execute("PRAGMA table_info(metrics)").fetchall()]
            # v1.5：真实抓取带来的账号级数据（旧版只有作品级合计）
            for col in ("followers", "total_likes", "works_count", "nick_name"):
                if col not in mcols:
                    c.execute("ALTER TABLE metrics ADD COLUMN %s %s" % (
                        col, "TEXT" if col == "nick_name" else "INTEGER"))

    def _rows(self, sql, args=()):
        with sqlite3.connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def get(self, vid):
        rows = self._rows("SELECT * FROM videos WHERE id=?", (vid,))
        return rows[0] if rows else None

    def list_videos(self):
        return self._rows("SELECT * FROM videos ORDER BY id DESC")

    def save_upload(self, raw_name, stream, length):
        """流式保存上传文件；返回 (video|None, error|None)。

        ★ 2026-10-06（安全审查 R5）：**读网络时不再持锁**。
          原来整个请求体的读取都在 `with self.lock` 里 —— 一个慢连接就能让
          配置保存、素材删除等所有用同一把锁的写操作一起卡住（写测试时实测到
          15 秒仍未返回）。改成：无锁流式写临时文件 → 完成后短暂加锁做 rename + 入库。
        """
        safe = safe_filename(raw_name)
        ext = os.path.splitext(safe)[1].lower()
        if ext not in VIDEO_EXTS:
            return None, "不支持的文件类型（支持 mp4/mov/m4v/webm/mkv/avi）"
        if length > MAX_UPLOAD_BYTES:
            return None, "文件超过上限（%d MB）" % (MAX_UPLOAD_BYTES // 1024 // 1024)
        # 临时文件名带随机后缀：原来写死 ".__uploading__.part"，并发上传会互相覆盖
        tmp = self.videos_dir / (".%s.part" % secrets.token_hex(8))
        got = 0
        try:
            with open(tmp, "wb") as f:
                while got < length:
                    chunk = stream.read(min(1024 * 1024, length - got))
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > MAX_UPLOAD_BYTES:      # 不能只信 Content-Length
                        return None, "文件超过上限（%d MB）" % (MAX_UPLOAD_BYTES // 1024 // 1024)
                    f.write(chunk)
            if got != length:
                return None, "上传中断（已收到 %d/%d 字节）" % (got, length)
            with self.lock:                     # ★ 只在这一小段持锁
                final = unique_name(self.videos_dir, safe)
                target = self.videos_dir / final
                os.replace(tmp, target)
                info = probe_mp4(target) if ext in (".mp4", ".mov", ".m4v") else {}
                with sqlite3.connect(self.db_path) as c:
                    cur = c.execute(
                        "INSERT INTO videos(name,stored,size,duration,width,height,added_at) VALUES(?,?,?,?,?,?,?)",
                        (final, final, got, info.get("duration"), info.get("width"), info.get("height"),
                         time.strftime("%Y-%m-%d %H:%M:%S")),
                    )
                    vid = cur.lastrowid
            return self.get(vid), None
        finally:
            tmp.unlink(missing_ok=True)         # 成功时已被 os.replace 拿走，这里是清理失败/中断

    def rename_video(self, vid, new_name):
        row = self.get(vid)
        if not row:
            return None, "素材不存在"
        safe = safe_filename(new_name)
        ext = os.path.splitext(safe)[1].lower()
        if not ext:
            safe += os.path.splitext(row["stored"])[1]
        elif ext not in VIDEO_EXTS:
            return None, "不支持的文件类型（改回视频格式）"
        with self.lock:
            old = self.videos_dir / row["stored"]
            if safe != row["stored"]:
                final = unique_name(self.videos_dir, safe)
                if old.exists():
                    os.replace(old, self.videos_dir / final)
                else:
                    final = safe
                with sqlite3.connect(self.db_path) as c:
                    c.execute("UPDATE videos SET name=?, stored=? WHERE id=?", (final, final, vid))
        return self.get(vid), None

    def delete_video(self, vid):
        row = self.get(vid)
        if not row:
            return False, "素材不存在"
        with self.lock:
            f = self.videos_dir / row["stored"]
            if f.exists():
                f.unlink()
            if row.get("cover"):
                cf = self.covers_dir / row["cover"]
                if cf.exists():
                    cf.unlink()
            with sqlite3.connect(self.db_path) as c:
                c.execute("DELETE FROM videos WHERE id=?", (vid,))
        return True, None

    # ---- 草稿（发布台当前文案） ----
    def get_draft(self):
        rows = self._rows("SELECT data, updated_at FROM drafts WHERE id=1")
        if not rows:
            return None
        try:
            data = json.loads(rows[0]["data"])
        except Exception:
            data = None
        return {"data": data, "updated_at": rows[0]["updated_at"]}

    def save_draft(self, data):
        s = json.dumps(data, ensure_ascii=False)
        with self.lock:
            with sqlite3.connect(self.db_path) as c:
                c.execute(
                    "INSERT INTO drafts(id,data,updated_at) VALUES(1,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                    (s, time.strftime("%Y-%m-%d %H:%M:%S")),
                )
        return self.get_draft()

    # ---- 封面 ----
    def set_cover(self, vid, stream, length, ext):
        """保存封面。

        ★ 2026-10-06 复核发现：视频上传改成无锁读取之后，封面这条**没跟着改** ——
          仍然是持着全局锁读整个网络请求体，慢封面照样卡住配置保存与其它写操作。
          现在与 save_upload 同一套：无锁流式写临时文件 → 完成后短暂加锁提交。
        """
        row = self.get(vid)
        if not row:
            return None, "素材不存在"
        if ext not in ("png", "jpg", "webp"):
            ext = "jpg"
        if length > MAX_COVER_BYTES:
            return None, "封面超过上限（%d MB）" % (MAX_COVER_BYTES // 1024 // 1024)
        # 临时名带随机后缀：原来写死 ".__cover.part"，并发封面会互相覆盖
        tmp = self.covers_dir / (".%s.part" % secrets.token_hex(8))
        got = 0
        try:
            with open(tmp, "wb") as f:
                while got < length:
                    chunk = stream.read(min(1024 * 1024, length - got))
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > MAX_COVER_BYTES:      # 不能只信 Content-Length
                        return None, "封面超过上限（%d MB）" % (MAX_COVER_BYTES // 1024 // 1024)
                    f.write(chunk)
            if got != length:
                return None, "上传中断"
            with self.lock:                        # ★ 只在这一小段持锁
                fname = "%d.%s" % (vid, ext)
                os.replace(tmp, self.covers_dir / fname)
                old = row.get("cover")
                if old and old != fname:
                    of = self.covers_dir / old
                    if of.exists():
                        of.unlink()
                with sqlite3.connect(self.db_path) as c:
                    c.execute("UPDATE videos SET cover=? WHERE id=?", (fname, vid))
        finally:
            tmp.unlink(missing_ok=True)            # 成功时已被 os.replace 拿走
        return self.get(vid), None

    # ---- 发布前预检（按平台规则表） ----
    def precheck(self, vid):
        row = self.get(vid)
        if not row:
            return None
        ext = os.path.splitext(row["stored"])[1].lower()
        out = []
        for key, r in RULES.items():
            issues = []
            if ext not in r.get("exts", []):
                issues.append("格式 %s 不在支持列表（%s）" % (ext or "未知", "、".join(r.get("exts", []))))
            dur = row.get("duration")
            md = r.get("max_duration")
            if md and dur is not None and dur > md:
                issues.append("时长 %s 超过上限 %s" % (fmt_sec(dur), fmt_sec(md)))
            ms = r.get("max_size_mb")
            if ms and row["size"] > ms * 1024 * 1024:
                issues.append("大小 %s 超过上限 %d MB" % (fmt_mb(row["size"]), ms))
            if dur is None and ext in (".mp4", ".mov", ".m4v"):
                issues.append("未读到时长信息，请留意")
            out.append({"key": key, "name": r.get("name", key), "ok": not issues,
                        "issues": issues, "recommend": r.get("recommend", "")})
        return out

    # ---- 账号（登录态） ----
    def list_accounts(self):
        rows = {r["key"]: r for r in self._rows("SELECT * FROM accounts")}
        out = []
        for key, r in RULES.items():
            shared = shared_login_key(key)
            if shared != key:
                # 共用登录态的平台：状态直接跟随被共用的那个（如西瓜跟抖音），
                # 并且从没登录过自己 —— 所以不显示出它自己的旧状态。
                srow = rows.get(shared, {})
                sname = RULES.get(shared, {}).get("name", shared)
                out.append({
                    "key": key,
                    "name": r.get("name", key),
                    "status": srow.get("status", "off"),
                    "note": ("与%s共用登录态" % sname) if srow.get("status") == "on"
                            else ("请先登录%s（两者共用登录态）" % sname),
                    "updated_at": srow.get("updated_at", ""),
                    # ★ 共用登录态的平台，最后核验时间跟随被共用的那个（如西瓜→抖音）
                    "verified_at": read_worker_status(self, shared).get("verified_at", ""),
                    "shared_with": shared,
                    "shared_with_name": sname,
                })
                continue
            row = rows.get(key, {})
            acc, acc_name = current_account(self, key)
            out.append({
                "key": key,
                "name": r.get("name", key),
                "status": row.get("status", "off"),
                "note": row.get("note", ""),
                "updated_at": row.get("updated_at", ""),
                # ★ 「最后核验时间」取自状态文件（--verify 真核过才写），不是 DB 的
                #   updated_at —— 后者只在状态/文案**变化**时才动：今天核验过、
                #   结果没变，它还停在昨天，用户看不出核验到底跑没跑。
                "verified_at": read_worker_status(self, key).get("verified_at", ""),
                "account": acc,
                "account_name": acc_name,
            })
        return out

    def set_account(self, key, status, note=""):
        with self.lock:
            with sqlite3.connect(self.db_path) as c:
                c.execute(
                    "INSERT INTO accounts(key,status,note,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at",
                    (key, status, note, time.strftime("%Y-%m-%d %H:%M:%S")),
                )

    def refresh_accounts(self):
        """把各 worker 状态文件同步进数据库"""
        cur = {r["key"]: r for r in self._rows("SELECT * FROM accounts")}
        for key in LOGIN_URLS:
            st = read_worker_status(self, key)
            state = st.get("state", "off")
            target = None
            if state == "on":
                target = ("on", st.get("note", "已登录"))
            elif state in ("opening", "waiting", "unknown"):
                # unknown＝页面没判准（加载中/异常）→ 一律按未登录显示，宁可让用户点兜底按钮
                target = ("waiting", st.get("note", "等待扫码"))
            elif state == "error":
                target = ("error", st.get("note", ""))
            elif state == "closed":
                target = ("off", "窗口已关闭")
            elif state == "off" and not st.get(UNREADABLE):
                # ★ 少了这一支的后果（2026-09-28 实测踩过）：`probe_accounts.py --verify`
                #   核出「未登录」并把状态文件写成 off 后，这里没有分支去接，target 保持
                #   None → set_account 根本不被调用 → 数据库/界面继续显示上一次的
                #   「已登录」。实测抖音掉登录后，发布台一直显示绿色，直到真去发布失败
                #   才改口。
                #   这是交接文档「坑 6」的镜像：坑 6 是「取消登录时乱写 off，把已登录的
                #   标成未登录」；这一支管的是另一半 ——「该认的 off 要认」。
                #   缺了它，「刷新状态」接不接 --verify 都没意义（接了界面也不会变）。
                #   回归测试：app/tests/test_15_accounts_sync.py
                #   note 用状态文件自带的（--verify 会写「已核验登录态：…」这种有用信息）；
                #   没有就留空 —— 徽章已经写着「未登录」了，再重复一遍是废话。
                #
                # ★ `not st.get(UNREADABLE)` 这一半是被 OCR 审出来的补丁（2026-09-28）：
                #   read_worker_status 在「文件在、但解析不出来」时也返回 off。
                #   那种 off 是**读错误**不是事实 —— 不加这个判断，一次瞬时读错误
                #   就能把已登录的账号标成未登录，而 /api/publish/create 也调本函数，
                #   用户点发布那一刻撞上会被直接拒掉。
                #   我当初补 off 分支时只想到「--verify 写出来的真 off」，漏了兜底那种。
                target = ("off", st.get("note") or "")
            if target:
                row = cur.get(key)
                if not row or row["status"] != target[0] or (row.get("note") or "") != target[1]:
                    self.set_account(key, target[0], target[1])

    # ---- 发布任务 ----
    def create_publish_batch(self, video_id, platforms, copies, mode, scheduled_at, mock):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            with sqlite3.connect(self.db_path) as c:
                row = c.execute("SELECT COALESCE(MAX(batch),0)+1 FROM publishes").fetchone()
                batch = row[0]
                for k in platforms:
                    cp = copies.get(k) or copies.get("common") or {}
                    c.execute(
                        "INSERT INTO publishes(batch,video_id,platform,title,body,topics,mode,scheduled_at,status,step,error,retries,mock,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (batch, video_id, k, str(cp.get("title") or ""), str(cp.get("body") or ""),
                         str(cp.get("topics") or ""), mode, scheduled_at, "pending", "排队中", "", 0,
                         1 if mock else 0, now, now),
                    )
        return batch

    def list_publishes(self, limit=300):
        return self._rows("SELECT * FROM publishes ORDER BY id DESC LIMIT ?", (limit,))

    def get_batch(self, batch):
        return self._rows("SELECT * FROM publishes WHERE batch=? ORDER BY id ASC", (batch,))

    def next_publish_job(self):
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        rows = self._rows(
            "SELECT * FROM publishes WHERE status='pending' AND (mode='now' OR (scheduled_at!='' AND scheduled_at<=?)) ORDER BY id ASC LIMIT 1",
            (now,),
        )
        return rows[0] if rows else None

    def update_job(self, jid, step=None, status=None, error=None):
        sets = ["updated_at=?"]
        args = [time.strftime("%Y-%m-%d %H:%M:%S")]
        if step is not None:
            sets.append("step=?")
            args.append(step)
        if status is not None:
            sets.append("status=?")
            args.append(status)
        if error is not None:
            sets.append("error=?")
            args.append(error)
        args.append(jid)
        with self.lock:
            with sqlite3.connect(self.db_path) as c:
                c.execute("UPDATE publishes SET %s WHERE id=?" % ",".join(sets), args)

    def retry_job(self, jid):
        rows = self._rows("SELECT * FROM publishes WHERE id=?", (jid,))
        if not rows:
            return None, "任务不存在"
        if rows[0]["status"] not in ("fail", "canceled", "manual"):
            return None, "该任务当前不可重试"
        with self.lock:
            with sqlite3.connect(self.db_path) as c:
                c.execute(
                    "UPDATE publishes SET status='pending', step='排队中（重试）', error='', retries=retries+1, updated_at=? WHERE id=?",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), jid),
                )
        return self._rows("SELECT * FROM publishes WHERE id=?", (jid,))[0], None

    def cancel_batch(self, batch):
        with self.lock:
            with sqlite3.connect(self.db_path) as c:
                c.execute(
                    "UPDATE publishes SET status='canceled', step='已取消', updated_at=? WHERE batch=? AND status='pending'",
                    (time.strftime("%Y-%m-%d %H:%M:%S"), batch),
                )
        return self.get_batch(batch)

    def batch_done(self, batch):
        rows = self.get_batch(batch)
        return all(r["status"] in ("success", "fail", "canceled", "manual") for r in rows), rows

    def batch_notified(self, batch):
        return bool(self._rows("SELECT 1 FROM notified_batches WHERE batch=?", (batch,)))

    def mark_batch_notified(self, batch):
        with self.lock:
            with sqlite3.connect(self.db_path) as c:
                c.execute("INSERT OR IGNORE INTO notified_batches(batch) VALUES(?)", (batch,))

    def auto_stop_platform(self, batch, platform):
        """同批次同平台已连续失败 ≥2 → 其余待执行的同平台任务自动停（维护机制）"""
        with self.lock:
            with sqlite3.connect(self.db_path) as c:
                n = c.execute("SELECT COUNT(*) FROM publishes WHERE batch=? AND platform=? AND status='fail'",
                              (batch, platform)).fetchone()[0]
                if n >= 2:
                    c.execute(
                        "UPDATE publishes SET status='canceled', step='已自动停', error='同平台连续失败，已自动停止（疑似平台改版，需维护）', updated_at=? "
                        "WHERE batch=? AND platform=? AND status='pending'",
                        (time.strftime("%Y-%m-%d %H:%M:%S"), batch, platform))
                    return True
        return False

    # ---- 数据指标 ----
    def upsert_metrics(self, day, platform, video_id, views, likes, comments, source,
                       followers=None, total_likes=None, works_count=None, nick_name=None):
        with self.lock:
            with sqlite3.connect(self.db_path) as c:
                row = c.execute("SELECT id FROM metrics WHERE day=? AND platform=? AND IFNULL(video_id,0)=IFNULL(?,0)",
                                (day, platform, video_id)).fetchone()
                now = time.strftime("%Y-%m-%d %H:%M:%S")
                if row:
                    c.execute("UPDATE metrics SET views=?,likes=?,comments=?,source=?,followers=?,"
                              "total_likes=?,works_count=?,nick_name=?,updated_at=? WHERE id=?",
                              (views, likes, comments, source, followers, total_likes,
                               works_count, nick_name, now, row[0]))
                else:
                    c.execute("INSERT INTO metrics(day,platform,video_id,views,likes,comments,source,"
                              "followers,total_likes,works_count,nick_name,updated_at) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              (day, platform, video_id, views, likes, comments, source,
                               followers, total_likes, works_count, nick_name, now))

    def metrics_board(self):
        rows = self._rows("SELECT * FROM metrics ORDER BY day DESC, id DESC")
        by_plat = {}
        for r in rows:
            k = r["platform"]
            if k not in by_plat:
                by_plat[k] = {"platform": k, "day": r["day"], "views": r["views"], "likes": r["likes"],
                              "comments": r["comments"], "source": r["source"], "updated_at": r["updated_at"],
                              "followers": r.get("followers"), "total_likes": r.get("total_likes"),
                              "works_count": r.get("works_count"), "nick_name": r.get("nick_name")}
        total = {"views": sum(v["views"] for v in by_plat.values()),
                 "likes": sum(v["likes"] for v in by_plat.values()),
                 "comments": sum(v["comments"] for v in by_plat.values())}
        last = rows[0]["updated_at"] if rows else ""
        return {"platforms": list(by_plat.values()), "total": total, "last_fetch": last}



# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):
    server_version = "VideoPub/" + APP_VERSION
    store: Store = None  # 由 main 注入

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ---- 鉴权 ----
    def _cookie_token(self):
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                return v.strip()
        return ""

    def _host_ok(self):
        """Host 头校验 —— 防 DNS rebinding（把恶意域名解析到本机 IP 再打进来）。

        放行规则：本机名 / IP 字面量 / 环境变量 VP_ALLOWED_HOSTS 里列出的域名。
        rebinding 攻击必须用一个**域名**，所以「只认 IP 字面量 + 白名单域名」足够挡住它。
        用域名访问（如挂在 Nginx 后面）时，把域名加进 VP_ALLOWED_HOSTS。
        """
        host = (self.headers.get("Host") or "").split(":")[0].strip().strip("[]").lower()
        if not host:
            return True                     # HTTP/1.0 客户端可能不带 Host
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", host) or ":" in host:
            return True                     # IP 字面量
        extra = [x.strip().lower() for x in (os.environ.get("VP_ALLOWED_HOSTS") or "").split(",") if x.strip()]
        return host in extra

    def _origin_allowed(self, value):
        """Origin/Referer 的值是否被允许。

        ★ Origin 的白名单**不能**照抄 _host_ok 的「任意 IP 字面量」规则：
          那条对 Host 成立（DNS rebinding 必须用域名，Host 是 IP 就不可能是 rebinding），
          但用在 Origin 上就等于放行任意攻击者 IP 上的页面。这里只认环回 + 显式白名单。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        try:
            u = urllib.parse.urlsplit(value)
        except Exception:                    # noqa: BLE001
            return False
        if not u.hostname:
            return False
        o = u.hostname.lower()
        op = u.port or (443 if u.scheme == "https" else 80)
        hh, _, hp = host.partition(":")
        try:
            hpnum = int(hp) if hp else 80
        except ValueError:
            hpnum = 80
        if o == hh.strip("[]") and op == hpnum:
            return True                      # 同源（host:port 一致）
        if o in ("localhost", "127.0.0.1", "::1"):
            return True                      # 本机
        extra = [x.strip().lower() for x in (os.environ.get("VP_ALLOWED_HOSTS") or "").split(",") if x.strip()]
        if o in extra:
            return True
        origins = [x.strip().lower().rstrip("/") for x in
                   (os.environ.get("VP_ALLOWED_ORIGINS") or "").split(",") if x.strip()]
        return value.strip().lower().rstrip("/") in origins

    def _origin_ok(self):
        """校验请求发起方。返回 True＝放行。

        ★ 2026-10-06（安全审查 R1）：Host 正确**不能**证明请求来自应用页面 ——
          任何网页都能让用户浏览器往 127.0.0.1 发请求，Host 也是对的。
          浏览器对跨站 POST 一定带 Origin（同源 POST 也带），所以「有 Origin 就必须对得上」
          能挡住整类跨站写请求。
        ★ 非浏览器调用方（本机登录助手 urllib、测试脚本）不发 Origin ——
          它们不是 CSRF 的载体：攻击者没法让别人的浏览器「不发 Origin」。
        ★ GET 不回退查 Referer：<img src> 加载封面/截图本来就不发 Origin，
          再查 Referer 会把页面自己的图片请求也拦掉，而 GET 没有副作用。
        """
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            if origin == "null":
                return False                 # 沙箱 iframe / data: 页面 —— 不是「没有来源」
            return self._origin_allowed(origin)
        if self.command == "POST":
            ref = (self.headers.get("Referer") or "").strip()
            if ref:
                return self._origin_allowed(ref)
        return True

    def _drain_briefly(self, budget=2.0, cap=8 * 1024 * 1024):
        """把请求体里还没读的字节读掉再关连接。

        ★ 为什么需要（2026-10-06，写 R5 测试时实测到）：
          不读就回包并关闭，接收缓冲区里还有未读数据 → 系统发 RST 而不是 FIN →
          客户端**收不到那个 413**，只看到"连接被中止"（WinError 10053）。
          nginx 的 lingering close 就是干这个的。
          用 cap 封顶：超大 body 不能在这里无限读下去。
        """
        try:
            self.connection.settimeout(budget)
        except OSError:
            return
        left = cap
        while left > 0:
            try:
                chunk = self.rfile.read(min(65536, left))
            except Exception:                    # noqa: BLE001  超时/已断开都到此为止
                break
            if not chunk:
                break
            left -= len(chunk)

    def _body_size_ok(self):
        """在**任何** handler 读 body 之前统一判体量 —— 一个判断点，不用改二十个调用处。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return True
        p = urllib.parse.urlparse(self.path).path
        cap = MAX_UPLOAD_BYTES if p in RAW_BODY_PATHS else MAX_JSON_BYTES
        return n <= cap

    def _json_ct_ok(self):
        """有 body 的 POST，除裸 body 接口外必须声明 application/json。

        ★ 浏览器跨站「简单请求」只能发 form-urlencoded / multipart / text/plain，
          永远发不出 application/json —— 这一条不读 body 就能把跨站写请求挡在门外，
          且不影响任何非浏览器调用方（它们本来就能设任意 Content-Type）。
        """
        if self.command != "POST":
            return True
        p = urllib.parse.urlparse(self.path).path
        if p in RAW_BODY_PATHS:
            return True
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return True
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json"

    def _gate(self):
        """返回 True＝放行；False＝已回包，调用方直接 return。"""
        p = urllib.parse.urlparse(self.path).path
        # ★ 来源与 Content-Type 校验放在**最前面** —— 连 AUTH_EXEMPT 里的
        #   登录、账号导入也要覆盖（审查报告明确要求"豁免接口也应纳入来源策略"）。
        if not self._origin_ok():
            self._json({"ok": False, "error": "请求来源不被允许"}, 403)
            return False
        if not self._json_ct_ok():
            self._json({"ok": False, "error": "JSON 接口要求 Content-Type: application/json"}, 415)
            return False
        if p in AUTH_EXEMPT or p.startswith("/ui/"):
            return True
        if not self._host_ok():
            self._json({"ok": False, "error": "Host 不在允许列表（如用域名访问，请设 VP_ALLOWED_HOSTS）"}, 403)
            return False
        try:
            auth = load_auth(self.store)
        except AuthBroken as e:
            # ★ 失败关闭：口令文件坏了就拒绝业务请求，**绝不退回免密码模式**
            #   （旧实现正是在这里因为 load_config 吞掉异常而放行）。
            self._json({"ok": False,
                        "error": "口令文件 auth.json 已损坏，拒绝服务。"
                                 "请在数据目录删除 auth.json 后重新设置访问口令（%s）" % e}, 503)
            return False
        if not (auth.get("口令哈希") or ""):
            return True                     # 未设口令＝本机模式；绑非本机地址时启动阶段已拒绝
        if session_ok(self._cookie_token(), auth.get("口令版本")):
            return True
        self._json({"ok": False, "error": "未登录", "need_login": True}, 401)
        return False

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if not self._gate():
            return
        if p == "/api/auth/status":
            # ★ 走免鉴权列表，所以这里要自己把「损坏」讲清楚 ——
            #   否则用户只会看到登录框反复拒绝，不知道该删哪个文件。
            try:
                auth = load_auth(self.store)
                auth_broken = False
            except AuthBroken:
                auth, auth_broken = {}, True
            return self._json({"ok": True,
                               "口令已设": bool(auth.get("口令哈希") or ""),
                               "配置损坏": auth_broken,
                               "logged_in": (not auth_broken) and session_ok(self._cookie_token(), auth.get("口令版本"))})
        if p == "/api/health":
            load_config(self.store)  # ★ 现读一次再报告 —— 状态是启动时定的，
                                     #   运行中被改坏的话不现读就会一直报"正常"
            update_status = {}
            if DESKTOP_MODE:
                try:
                    update_status = json.loads((self.store.data_dir / "update.status.json").read_text(encoding="utf-8"))
                    if not isinstance(update_status, dict):
                        update_status = {}
                except (OSError, ValueError, UnicodeError):
                    pass
            return self._json({"ok": True, "app": "短视频发布工具", "version": APP_VERSION,
                               "mode": "desktop" if DESKTOP_MODE else "server",
                               "login_mode": "local" if DESKTOP_MODE or sys.platform == "win32" else "qr",
                               "instance_id": DESKTOP_INSTANCE_ID, "data_id": DESKTOP_DATA_ID,
                               "pid": os.getpid(), "port": CURRENT_PORT, "update": update_status,
                               "novnc_port": 0 if DESKTOP_MODE else novnc_port(),
                               "display": display_ok(),
                               # 配置损坏状态：界面据此提示"已备份 + 已重置为默认值"
                               "配置损坏": CONFIG_STATE["损坏"],
                               "配置备份": CONFIG_STATE["备份"]})
        if p == "/api/videos":
            return self._json({"ok": True, "videos": self.store.list_videos()})
        if p == "/api/draft":
            return self._json({"ok": True, "draft": self.store.get_draft()})
        if p == "/api/accounts":
            self.store.refresh_accounts()
            maybe_probe_accounts(self.store)   # 已登录但还没账号 ID 的，后台补探一次
            # verify：各平台的"核验登录态"进度（界面据此显示"核验中/已完成"）
            # ★ 读也要加锁（OCR 复查指出）：服务是 ThreadingHTTPServer，
            #   后台线程正在改这个 dict，不加锁就是数据竞争。
            with VERIFY_LOCK:
                _vsnap = dict(VERIFY_STATE)
            return self._json({"ok": True, "accounts": self.store.list_accounts(),
                               "verify": _vsnap})
        if p == "/api/config":
            return self._json({"ok": True,
                               "config": public_config(load_config(self.store), load_auth(self.store))})
        if p == "/api/ai/status":
            cfg = load_ai_config(self.store)
            # ★ 只报「配没配」，绝不回传 api_key 本身 —— 前端拿不到密钥。
            return self._json({"ok": True, "enabled": bool(cfg.get("enabled")),
                               "configured": bool(str(cfg.get("api_key") or "").strip()),
                               "model": str(cfg.get("model") or ""),
                               "api_base": str(cfg.get("api_base") or ""), "mock": AI_MOCK})
        if p == "/api/data-info":
            vids = self.store.list_videos()
            files, size = dir_stats(self.store.data_dir)
            return self._json({"ok": True, "dir": str(self.store.data_dir),
                               "files": files, "size": size, "videos": len(vids)})
        if p == "/api/logs/tail":
            q = urllib.parse.parse_qs(u.query)
            try:
                lines = int((q.get("lines") or ["200"])[0])
            except Exception:
                lines = 200
            lines = max(1, min(1000, lines))
            lp = APP_DIR / "server.out.log"
            text = ""
            if lp.exists():
                try:
                    raw = lp.read_text(encoding="utf-8", errors="ignore")
                    text = "\n".join(raw.splitlines()[-lines:])
                except OSError:
                    text = ""
            if not text:
                text = "（暂无日志：服务在窗口模式运行时日志显示在窗口里；后台模式会写到 app/server.out.log）"
            return self._json({"ok": True, "text": text})
        if p == "/api/publish/batches":
            rows = self.store.list_publishes()
            batches = {}
            for r in rows:
                b = batches.setdefault(r["batch"], {"batch": r["batch"], "created_at": r["created_at"], "jobs": []})
                b["jobs"].append(r)
            ordered = sorted(batches.values(), key=lambda x: x["batch"], reverse=True)
            return self._json({"ok": True, "batches": ordered[:50]})
        if p == "/api/publish/batch":
            q = urllib.parse.parse_qs(u.query)
            try:
                batch = int((q.get("id") or ["0"])[0])
            except Exception:
                batch = 0
            rows = self.store.get_batch(batch)
            if not rows:
                return self._json({"ok": False, "error": "批次不存在"}, 404)
            # engine_wait：引擎正卡在「发布间隔」里时带上剩余秒数，
            # 界面据此把「排队中」显示成「等待发布间隔 ~Ns」，免得当成卡死。
            return self._json({"ok": True, "batch": batch, "jobs": rows,
                               "engine_wait": engine_wait_state()})
        if p == "/api/records":
            rows = self.store.list_publishes(1000)
            for r in rows:
                r["has_shot"] = os.path.exists(str(self.store.data_dir / "publish" / ("job_%d.png" % r["id"])))
            done = [r for r in rows if r["status"] in ("success", "fail", "manual", "canceled")]
            succ = len([r for r in rows if r["status"] == "success"])
            failn2 = len([r for r in rows if r["status"] == "fail"])
            rate = int(round(succ * 100.0 / len(done))) if done else 0
            return self._json({"ok": True, "records": rows,
                               "stats": {"total": len(rows), "success": succ, "fail": failn2, "rate": rate}})
        if p == "/api/metrics":
            b = self.store.metrics_board()
            return self._json({"ok": True, "platforms": b["platforms"], "total": b["total"],
                               "last_fetch": b["last_fetch"],
                               "supported": list(DATA_WORKER_SUPPORTED),
                               "fetch": dict(FETCH_STATE)})
        if p == "/api/publish/export":
            rows = self.store.list_publishes(2000)
            # 表头不含外部内容，保持原样；数据行全部过 csv_cell（含公式前缀中和）
            lines = ["时间,批次,视频ID,平台,标题,状态,说明"]
            for r in rows:
                lines.append(",".join(csv_cell(x) for x in (
                    r["created_at"], r["batch"], r["video_id"],
                    RULES.get(r["platform"], {}).get("name", r["platform"]),
                    r["title"], r["status"], r["error"])))
            body = ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")
            return self._send(200, body, "text/csv; charset=utf-8")
        if p.startswith("/api/publish/shot/"):
            try:
                jid = int(p.rsplit("/", 1)[1])
            except Exception:
                jid = 0
            f = self.store.data_dir / "publish" / ("job_%d.png" % jid)
            return self._stream_file(f)
        if p == "/mock/login":
            # 带上真实的登录页特征词，好让 platform_login.classify 走和真平台同一条判据
            html = ("<meta charset='utf-8'><meta http-equiv='refresh' content='2;url=/mock/logged'>"
                    "<h3>模拟登录页</h3>"
                    "<p>请使用手机 App 扫描下方二维码</p>"
                    "<p>扫码登录 ｜ 验证码登录 ｜ 密码登录</p>")
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if p == "/mock/logged":
            html = ("<meta charset='utf-8'><h3>已登录（模拟）</h3>"
                    "<p>这里是创作中心首页，你已经登录成功，可以开始发布视频了。</p>"
                    "<p>菜单：发布视频 ｜ 内容管理 ｜ 数据中心 ｜ 账号设置</p>")
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if p == "/mock/publish":
            # 模拟发布页：选择器与抖音真机一致，用来在没有真实登录态时回归发布流程
            html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>body{font:14px/1.7 "Microsoft YaHei";padding:24px}
input,button{font:14px/1.7 "Microsoft YaHei";padding:6px 10px;margin:6px 0}
#body{min-height:120px;border:1px solid #ccc;padding:8px;width:520px}
#pub{background:#000;color:#fff;border:0;padding:9px 26px}</style></head><body>
<div id="logged-banner">创作中心 · 发布视频 · 内容管理 · 数据中心 · 退出登录</div>
<h3>作品发布</h3>
<div style="height:1400px">（模拟长页面：发布按钮在页面底部，用来验证会不会滚到视野内）</div>
<label>上传视频：<input type="file" accept="video/mp4,video/*,.mp4,.mov"></label><br>
<label>标题：<input type="text" placeholder="填写作品标题，为作品获得更多流量"></label><br>
<div>作品描述</div>
<div id="body" contenteditable="true" class="zone-container"></div>
<br><button id="pub" onclick="MOCKCAPTCHA ? showCaptcha() : location.href='/mock/published'">发布</button><button>暂存离开</button>
<script>
var MOCKCAPTCHA = location.search.indexOf('captcha=1') >= 0;
function showCaptcha(){
  var d = document.createElement('div');
  d.id = 'captcha-dialog';
  d.innerHTML = '<h3>接收短信验证码</h3><p>为确保是本人操作抖音账号，请输入收到的短信验证码</p>'
              + '<input placeholder="请输入验证码"><button>获取验证码</button><button>验证</button>';
  document.body.appendChild(d);
}
</script>
</body></html>"""
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if p == "/mock/published":
            # 只有「真的点了提交」才会走到这里。测试据此断言工具没有越界提交。
            try:
                (self.store.data_dir / "_mock_submitted.txt").write_text(
                    time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            except OSError:
                pass
            return self._send(200, "<meta charset='utf-8'><h3>已提交（模拟）</h3>".encode("utf-8"),
                              "text/html; charset=utf-8")
        if p == "/api/accounts/qr/status":
            q = urllib.parse.parse_qs(u.query)
            key = (q.get("key") or [""])[0]
            if key not in LOGIN_URLS:
                return self._json({"ok": False, "error": "未知平台"}, 400)
            st = qr_read_status(self.store, key)
            # 收尾导入（幂等）。★ 这条只是「顺路也做一次」—— 真正兜底的是
            #   start_qr_login 起的看门线程，所以前端不轮询也不会漏（实测踩过）。
            n = maybe_finish_qr_login(self.store, key)
            if n is not None:
                st = qr_read_status(self.store, key)
                st["imported"] = n
            return self._json({"ok": True, "status": st, "alive": qr_alive(key)})
        if p == "/api/accounts/qr/shot":
            # 服务端浏览器（容器里 Xvfb 上那个）当前画面的截图。
            # ★ 用途：登录卡在某一步时，用户能直接看到页面上到底有什么按钮，
            #   而不是只能看一行文字提示（比如「没找到获取验证码按钮」）。
            q = urllib.parse.parse_qs(u.query)
            key = (q.get("key") or [""])[0]
            tag = (q.get("tag") or ["sms_input"])[0]
            if key not in LOGIN_URLS or not re.match(r"^[a-z_]{1,20}$", tag):
                return self._send(400, b"bad args", "text/plain; charset=utf-8")
            return self._stream_file(qr_work_dir(self.store, key) / ("state_%s.png" % tag))
        if p == "/api/accounts/qr/qrcode":
            q = urllib.parse.parse_qs(u.query)
            key = (q.get("key") or [""])[0]
            if key not in LOGIN_URLS:
                return self._send(400, b"bad key", "text/plain; charset=utf-8")
            f = qr_work_dir(self.store, key) / "qr.png"
            return self._stream_file(f)
        if p == "/api/videos/precheck":
            q = urllib.parse.parse_qs(u.query)
            try:
                vid = int((q.get("id") or ["0"])[0])
            except Exception:
                vid = 0
            v = self.store.get(vid)
            if not v:
                return self._json({"ok": False, "error": "素材不存在"}, 404)
            return self._json({"ok": True, "video": v, "platforms": self.store.precheck(vid)})
        parts = [seg for seg in p.split("/") if seg]
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "videos" and parts[2] in ("raw", "cover"):
            try:
                vid = int(parts[3])
            except Exception:
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            return self._serve_media(vid, parts[2])
        if p == "/favicon.ico":
            return self._send(200, b"", "image/x-icon")
        if p == "/" or p == "/index.html":
            return self._file(UI_DIR / "index.html")
        if p.startswith("/ui/"):
            rel = urllib.parse.unquote(p[4:])
            try:
                target = (UI_DIR / rel).resolve()
                if not target.is_relative_to(UI_DIR.resolve()):
                    raise ValueError
            except Exception:
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            return self._file(target)
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    # ---- POST ----
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path
        if not self._gate():
            return
        # ★ 体量在进任何 handler 之前统一判掉（R5）：不能等 handler 自己读了 body 才发现超大。
        if not self._body_size_ok():
            # 先把 body 排空再回 413，否则客户端收到的是连接中止而不是这条错误
            self._json({"ok": False, "error": "请求体过大"}, 413)
            self._drain_briefly()
            return
        try:
            self.connection.settimeout(JSON_TIMEOUT)
        except OSError:
            pass
        if p == "/api/accounts/verify":
            # ★ 2026-09-29：这个分支**必须在 do_POST**里 —— 界面用 apiPost 调它
            #   （第一版我错放进了 do_GET，端点等于不存在：点了没反应）。
            #   顺手把它做成有副作用的动作（真开浏览器），语义上也该是 POST。
            d = self._read_json()
            key = str(d.get("key") or "")
            if key:
                if key not in LOGIN_URLS:
                    return self._json({"ok": False, "error": "未知平台：%s" % key}, 400)
                keys = [key]
            else:
                keys = list(LOGIN_URLS)
            okv, err = verify_login_state(self.store, keys)
            return self._json({"ok": okv, "error": err, "verifying": keys if okv else []},
                              200 if okv else 400)
        if p == "/api/videos/upload":
            q = urllib.parse.parse_qs(u.query)
            name = (q.get("name") or ["未命名.mp4"])[0]
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._json({"ok": False, "error": "空文件"}, 400)
            if length > MAX_UPLOAD_BYTES:
                return self._json({"ok": False, "error": "文件超过上限（%d MB）"
                                   % (MAX_UPLOAD_BYTES // 1024 // 1024)}, 413)
            # 并发上限：慢上传不再持锁了，但也不能无限占线程
            if not UPLOAD_SLOTS.acquire(blocking=False):
                return self._json({"ok": False, "error": "同时上传数已达上限，请稍后重试"}, 429)
            try:
                try:
                    self.connection.settimeout(UPLOAD_TIMEOUT)
                except OSError:
                    pass
                vid, err = self.store.save_upload(name, self.rfile, length)
            finally:
                UPLOAD_SLOTS.release()
            if err:
                return self._json({"ok": False, "error": err}, 400)
            return self._json({"ok": True, "video": vid})
        if p == "/api/videos/rename":
            d = self._read_json()
            try:
                vid = int(d.get("id", 0))
            except Exception:
                vid = 0
            v, err = self.store.rename_video(vid, str(d.get("name", "")))
            if err:
                return self._json({"ok": False, "error": err}, 400)
            return self._json({"ok": True, "video": v})
        if p == "/api/videos/delete":
            d = self._read_json()
            try:
                vid = int(d.get("id", 0))
            except Exception:
                vid = 0
            okdele, err = self.store.delete_video(vid)
            if not okdele:
                return self._json({"ok": False, "error": err}, 400)
            return self._json({"ok": True})
        if p == "/api/draft/save":
            d = self._read_json()
            data = d.get("data")
            if not isinstance(data, dict):
                return self._json({"ok": False, "error": "数据格式不对"}, 400)
            if len(json.dumps(data, ensure_ascii=False)) > 512 * 1024:
                return self._json({"ok": False, "error": "草稿过大"}, 400)
            res = self.store.save_draft(data)
            return self._json({"ok": True, "draft": res})
        if p == "/api/accounts/login":
            d = self._read_json()
            key = str(d.get("key", ""))
            if key not in LOGIN_URLS:
                return self._json({"ok": False, "error": "未知平台"}, 400)
            # 共用登录态的平台（西瓜=抖音）→ 打开被共用那个平台的登录窗，
            # 不能给它自己开 —— 同一个 profile 开第二个浏览器必崩（实测）。
            target = shared_login_key(key)
            if target != key:
                sname = RULES.get(target, {}).get("name", target)
                mock = bool(d.get("mock")) or (target in MOCK_LOGIN_KEYS)
                info = start_worker(self.store, target, mock=mock)
                return self._json({"ok": True, "status": info, "shared_with": target,
                                   "note": "%s 与 %s 共用登录态，已打开 %s 的登录窗"
                                           % (RULES.get(key, {}).get("name", key), sname, sname)})
            mock = bool(d.get("mock")) or (key in MOCK_LOGIN_KEYS)
            info = start_worker(self.store, key, mock=mock)
            return self._json({"ok": True, "status": info})
        if p == "/api/accounts/close":
            d = self._read_json()
            key = str(d.get("key", ""))
            if key not in LOGIN_URLS:
                return self._json({"ok": False, "error": "未知平台"}, 400)
            target = shared_login_key(key)
            info = stop_worker(self.store, target)
            prev = read_worker_status(self.store, target)
            self.store.set_account(target, info["state"], prev.get("note", ""))
            return self._json({"ok": True})
        if p == "/api/accounts/confirm":
            # 兜底通道：自动探测没认出来时，用户点「我已登录完成」
            d = self._read_json()
            key = str(d.get("key", ""))
            if key not in LOGIN_URLS:
                return self._json({"ok": False, "error": "未知平台"}, 400)
            key = shared_login_key(key)
            had_local_worker = worker_alive(key)
            if worker_alive(key):
                send_worker_cmd(self.store, key, "confirm")
                for _ in range(50):          # 等浏览器优雅关闭、登录态落盘后才报告成功
                    if not worker_alive(key):
                        break
                    time.sleep(0.4)
            local = read_worker_status(self.store, key)
            if had_local_worker or local.get("login_mode") == "local" or (local.get("state") == "on" and local.get("browser_closed")):
                if worker_alive(key):
                    return self._json({"ok": False, "error": "登录态仍在保存，请稍后再试"}, 409)
                if local.get("state") != "on" or not local.get("browser_closed"):
                    return self._json({"ok": False, "error": local.get("note") or "登录窗未正常完成保存"}, 400)
                self.store.refresh_accounts()
                return self._json({"ok": True, "status": local, "imported": None})
            # ★ 必须先收尾导入，再写「已登录」。
            #   原来的写法是**无条件**写 on，却从不导入登录态 —— 实测踩过：
            #   微博/B站就是这样被标成「已确认登录（手动）」，但 profile 是空的
            #   （cookie 明明已导出到 qrlogin/<key>/state.json），
            #   于是账号页显示绿色、发布时却说没登录。
            #   用户点这颗按钮的本意就是「我登好了，你收一下」，收尾正是该做的事。
            n = maybe_finish_qr_login(self.store, key)
            if n is None:
                # 没有可导入的登录态（worker 没报成功 / 状态文件不是 done）→
                # 按用户的意图标成已登录 —— 这颗按钮本来就是这个用途：
                # 「自动探测没认出来时，用户说他已经登好了」。
                # ★ 必须【状态文件 + 数据库】两处都写：只写数据库没用 ——
                #   /api/accounts 会调 refresh_accounts()，它从状态文件读回
                #   waiting 把数据库覆盖掉，按钮看着像没反应（测出来过）。
                #   同理要写状态文件，refresh_accounts 才会把「已登录」同步下去。
                write_worker_status(self.store, key,
                                    {"key": key, "state": "on", "url": "",
                                     "note": "已确认登录（手动）"})
                self.store.set_account(key, "on", "已确认登录（手动）")
            return self._json({"ok": True, "status": {"key": key, "state": "on"},
                               "imported": n})
        if p == "/api/config/save":
            d = self._read_json()
            dataset = d.get("data") or {}
            # ★ 2026-10-06（安全审查 R2/R3）：对外监听时禁止**运行中**清空最后一道凭证。
            #   这个约束原来只在启动时检查，运行中把口令清空就退回免密码模式
            #   —— 审查报告已动态确认清空后业务接口放行。
            if "口令" in dataset and not str(dataset.get("口令") or ""):
                if not SERVER_IS_LOOPBACK:
                    return self._json({"ok": False,
                                       "error": "对外提供服务时不能清空访问口令"}, 400)
            # ★ 口令变更与版本递增在 save_config 内部**一次写完**（见其 docstring）
            cfg, auth_new, pw_changed = save_config(self.store, dataset)
            # ★ 改口令的人当场就是主人 —— 设完立刻给他一个会话。
            #   否则用户刚设完口令，设置页自己就 401 被锁在外（实测踩过）。
            if pw_changed and cfg.get("口令已设"):
                # 版本已随哈希一起 +1 → 所有旧会话立即失效（R3）；操作人拿新版本的会话。
                tok = new_session(auth_new.get("口令版本"))
                body = json.dumps({"ok": True, "config": cfg}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Set-Cookie",
                                 "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                                 % (SESSION_COOKIE, tok, SESSION_TTL))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            return self._json({"ok": True, "config": cfg})
        if p == "/api/auth/login":
            d = self._read_json()
            pw = str(d.get("pass", ""))
            # ★ 2026-10-06 复核发现：原来检查密码读一次 auth、发会话又读一次 ——
            #   两次之间若有人改了口令，用**旧密码**也能拿到一个带新版本号的会话，
            #   于是改口令之后它照样能用。必须用同一份快照。
            auth_snap = load_auth(self.store)
            if not check_password(auth_snap, pw):
                time.sleep(0.8)          # 轻微延迟，挡一下暴力猜口令
                return self._json({"ok": False, "error": "口令不对"}, 401)
            tok = new_session(auth_snap.get("口令版本"))
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # HttpOnly：JS 读不到，降低 XSS 窃取风险
            # SameSite=Lax：跨站发起的 POST 不会带上它 → 顺带挡住 CSRF / DNS-rebinding
            self.send_header("Set-Cookie",
                             "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
                             % (SESSION_COOKIE, tok, SESSION_TTL))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/api/auth/logout":
            tok = self._cookie_token()
            if tok:
                SESSIONS.pop(tok, None)
            return self._json({"ok": True})
        if p == "/api/auth/check":
            # 兼容旧前端：只报「有没有设口令 / 当前是否已登录」，不再做明文比对
            return self._json({"ok": True, "match": session_ok(self._cookie_token(), load_auth(self.store).get("口令版本")),
                               "口令已设": bool(load_auth(self.store).get("口令哈希") or "")})
        if p == "/api/open-folder":
            try:
                os.startfile(str(self.store.data_dir))
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:120]}, 400)
        if p == "/api/security/harden":
            ok2, detail = harden_data_dir(self.store)
            return self._json({"ok": ok2, "detail": detail}, 200 if ok2 else 400)
        if p == "/api/ai/save":
            d = self._read_json()
            # 兼容旧前端：只发 {enabled} 时等价于原来的行为。
            # 新前端可一并发 api_base / model / api_key（留空＝不改动那一项）。
            cfg = save_ai_config(self.store, d)
            return self._json({"ok": True, "enabled": bool(cfg.get("enabled")),
                               "configured": bool(str(cfg.get("api_key") or "").strip()),
                               "model": str(cfg.get("model") or "")})
        if p == "/api/ai/generate":
            d = self._read_json()
            title = str(d.get("title") or "")[:300]
            body = str(d.get("body") or "")[:3000]
            topics = str(d.get("topics") or "")[:400]
            if not (title or body):
                return self._json({"ok": False, "error": "先在「通用」里写标题或正文"})
            copies, err = ai_generate(self.store, title, body, topics)
            if err:
                return self._json({"ok": False, "error": err})
            return self._json({"ok": True, "copies": copies, "engine": "ai"})
        if p == "/api/accounts/import":
            # 登录助手（本机运行的 exe）把登录态传上来。
            # ★ 它没有会话 Cookie，所以在这里**用口令自证**（而不是靠 _gate 的会话校验）。
            d = self._read_json()
            key = str(d.get("key", ""))
            if key not in LOGIN_URLS:
                return self._json({"ok": False, "error": "未知平台"}, 400)
            cfg_imp = load_auth(self.store)
            # ★ 没设口令时必须拒绝。check_password 在「没设口令」时一律返回 True
            #   —— 那是本机模式的约定，但**上传登录态**这种接口不能沿用：
            #   否则内网里任何人都能把一份登录态推上来（实测时先踩到的就是这个）。
            if not (cfg_imp.get("口令哈希") or ""):
                return self._json({"ok": False,
                                   "error": "服务器还没设访问口令，拒绝接收上传。"
                                            "请先在设置页设一个口令"}, 403)
            if not check_password(cfg_imp, str(d.get("password", ""))):
                time.sleep(0.8)
                return self._json({"ok": False, "error": "访问口令不对"}, 401)
            state = d.get("state")
            if not isinstance(state, dict) or not (state.get("cookies")):
                return self._json({"ok": False, "error": "登录态内容为空"}, 400)
            st_path = qr_work_dir(self.store, key) / "imported_state.json"
            try:
                st_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                n = sau_bridge.import_storage_state(browser_profile_dir(self.store, key), st_path)
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": "导入失败：%s" % str(e)[:150]}, 400)
            acc, acc_name = "", ""
            try:
                # 顺手探一下账号身份（配额要按账号算）
                stt = qr_read_status(self.store, key)
                acc, acc_name = str(stt.get("account") or ""), str(stt.get("account_name") or "")
            except Exception:
                pass
            write_worker_status(self.store, key,
                                {"key": key, "state": "on", "url": "",
                                 "note": "由登录助手上传", "account": acc,
                                 "account_name": acc_name,
                                 "detail": "已从本机登录助手导入 %d 条 cookie" % n})
            self.store.set_account(key, "on", "由登录助手上传")
            return self._json({"ok": True, "cookies": n, "account": acc, "account_name": acc_name})
        if p == "/api/accounts/qr/start":
            d = self._read_json()
            key = str(d.get("key", ""))
            if key not in LOGIN_URLS:
                return self._json({"ok": False, "error": "未知平台"}, 400)
            # 走模拟登录的 key（自测用）不接可交互扫码 —— 那是真打平台页面的。
            # fallback=True 告诉前端「回落本机登录窗那条路」。
            if key in MOCK_LOGIN_KEYS:
                return self._json({"ok": False, "fallback": True,
                                   "error": "该平台本次运行走模拟登录"}, 400)
            # ★ 不再用 sau_bridge.available() 当闸门（2026-09-27 拆掉）：
            #   那返回的是「SAU 有上传器的平台」，只有 5 个 —— 头条号/B 站被挡在外面，
            #   用户只能回本机开登录窗。可扫码登录这条链路**跟 SAU 已经无关**了：
            #   worker 自己起浏览器、用 platform_login 判登录态、成功了收 storage_state。
            #   平台清单就是上面的 LOGIN_URLS，8 个全支持。
            info = start_qr_login(self.store, key)
            return self._json({"ok": True, "status": info})
        if p == "/api/accounts/qr/cmd":
            d = self._read_json()
            key = str(d.get("key", ""))
            cmd = str(d.get("cmd", ""))[:200]
            if key not in LOGIN_URLS:
                return self._json({"ok": False, "error": "未知平台"}, 400)
            ok2 = qr_send_cmd(self.store, key, cmd)
            return self._json({"ok": bool(ok2)})
        if p == "/api/accounts/qr/cancel":
            d = self._read_json()
            key = str(d.get("key", ""))
            if key not in LOGIN_URLS:
                return self._json({"ok": False, "error": "未知平台"}, 400)
            qr_send_cmd(self.store, key, "cancel")
            time.sleep(1.5)
            # ★ 必须走 kill_worker（连进程组一起收）：正常情况 worker 收到 cancel 指令
            #   会自己关浏览器退出，但 1.5 秒内没退干净的话，光 terminate worker
            #   会把浏览器漏成孤儿 —— 用户每点一次取消就漏一个。实测踩过。
            kill_worker(QR_WORKERS.pop(key, None))
            # ★ 取消之后不许再收尾导入：worker 被强杀时进程退出，看门线程会醒过来；
            #   若此时状态文件恰好停在 done，它就会把登录态灌进去并写成「已登录」，
            #   把用户明确表达的「取消」覆盖掉。这里先占住 QR_IMPORTED 把它挡在门外
            #   （下一次 start_qr_login 会 discard，不影响重新登录）。
            with QR_LOCK:
                QR_IMPORTED.add(key)
            # ★ 还原成开工前的状态，**不能无条件写 off**：用户可能只是中止一次重登，
            #   他原本的登录态（profile 里的 cookie）根本没动。实测踩过 ——
            #   对已登录的抖音点了取消，账号直接被标成未登录。
            prev_status, prev_note = QR_PREV_STATUS.pop(key, ("off", ""))
            # ★ 状态文件也要一起还原（OCR 2026-09-28 指出）：在途的那次收尾导入
            #   （可能刚好赶在 kill 之前跑完）会把**状态文件**写成「扫码登录成功」，
            #   而这里只改数据库 —— 于是文件说 on、库说 prev，两边打架，
            #   下次 refresh_accounts 又会把库拽回 on，用户点的「取消」等于没点。
            #   两处一起写，才真的还原成开工前。
            write_worker_status(self.store, key, {"key": key, "state": prev_status,
                                                  "url": "", "note": prev_note})
            self.store.set_account(key, prev_status, prev_note)
            return self._json({"ok": True})
        if p == "/api/publish/create":
            d = self._read_json()
            try:
                vid = int(d.get("video_id") or 0)
            except Exception:
                vid = 0
            if not self.store.get(vid):
                return self._json({"ok": False, "error": "请先选择视频"}, 400)
            platforms = [k for k in (d.get("platforms") or []) if k in LOGIN_URLS]
            if not platforms:
                return self._json({"ok": False, "error": "请先勾选至少一个平台"}, 400)
            # ★ 登录检查放在配额检查**前面**。实测踩过：用户其实已经掉线，
            #   却只看到「达到日更上限」，完全找不到真正原因。
            mock = bool(d.get("mock"))
            # ★ 封面硬闸（2026-09-30）：微博的上传器**没封面直接 raise**
            #   （`vendor/sau/uploader/weibo_uploader/main.py` 的 validate_upload_args：
            #   「微博视频发布必须提供封面图」），所以在这里就拦住并说清怎么解决 ——
            #   别等跑完一整轮才失败。界面侧会先自动抽第 1 帧兜底（app.js 的 autoCover），
            #   这一层是**权威闸**：防绕过界面直接调接口、以及抽帧失败那种情况。
            if not mock and "weibo" in platforms:
                _vrow = self.store.get(vid) or {}
                if not _vrow.get("cover"):
                    return self._json({"ok": False,
                                       "error": "微博必须有封面：请先在素材页给这条视频设一张"
                                                "（点「设置封面」→ 拖到想要的画面 → 截取当前帧）"}, 400)
            if not mock:
                self.store.refresh_accounts()
                stmap = {a["key"]: a["status"] for a in self.store.list_accounts()}
                not_on = [RULES.get(k, {}).get("name", k) for k in platforms if stmap.get(k) != "on"]
                if not_on:
                    return self._json({"ok": False,
                                       "error": "以下平台未登录，请先到账号页扫码：" + "、".join(not_on)}, 400)
            today = time.strftime("%Y-%m-%d")
            try:
                limit = int(load_config(self.store).get("单账号日更上限") or 2)
            except Exception:
                limit = 2
            # ★ 只统计**真正会占用发布名额**的状态。
            #   旧写法是 `status!='canceled'` —— 于是**失败**的任务也占配额，
            #   结果几次失败就把用户锁死一整天（实测踩过：两条 fail 让日更上限 2 直接满）。
            #   失败意味着根本没发出去，不该算数；已取消同理。
            COUNTED = ("success", "manual", "pending", "running")
            over = []
            for k in set(platforms):
                # ★ 按**账号**算，不是按平台。拿到账号身份就只数这个账号的；
                #   拿不到（该平台还没实现身份探测）才退回按平台数，并在提示里说明。
                acc, acc_name = current_account(self.store, k)
                if acc:
                    sql = ("SELECT COUNT(*) AS n FROM publishes WHERE platform=? AND substr(created_at,1,10)=? "
                           "AND status IN (%s) AND account=?" % ",".join("?" * len(COUNTED)))
                    args = (k, today) + COUNTED + (acc,)
                    scope = "账号「%s」" % (acc_name or acc)
                else:
                    sql = ("SELECT COUNT(*) AS n FROM publishes WHERE platform=? AND substr(created_at,1,10)=? "
                           "AND status IN (%s)" % ",".join("?" * len(COUNTED)))
                    args = (k, today) + COUNTED
                    scope = "该平台（未识别到账号，按平台计）"
                n0 = self.store._rows(sql, args)
                used = n0[0]["n"] if n0 else 0
                n = used + platforms.count(k)
                if n > limit:
                    over.append("%s %s 今日已发 %d 条" % (RULES.get(k, {}).get("name", k), scope, used))
            if over:
                return self._json({"ok": False, "error": "今日已到单账号日更上限（%d 条/天）：%s；可明日再发或到设置调整" % (limit, "、".join(over))}, 400)
            mode = "schedule" if d.get("mode") == "schedule" else "now"
            scheduled_at = str(d.get("scheduled_at") or "") if mode == "schedule" else ""
            if mode == "schedule" and not scheduled_at:
                return self._json({"ok": False, "error": "请选择定时时间"}, 400)
            # （登录检查已前移到配额检查之前 —— 见上面）
            copies = d.get("copies") if isinstance(d.get("copies"), dict) else {}
            batch = self.store.create_publish_batch(vid, platforms, copies, mode, scheduled_at, mock)
            return self._json({"ok": True, "batch": batch, "count": len(platforms)})
        if p == "/api/publish/retry":
            d = self._read_json()
            try:
                jid = int(d.get("id") or 0)
            except Exception:
                jid = 0
            job, err = self.store.retry_job(jid)
            if err:
                return self._json({"ok": False, "error": err}, 400)
            return self._json({"ok": True, "job": job})
        if p == "/api/publish/cancel":
            d = self._read_json()
            try:
                batch = int(d.get("batch") or 0)
            except Exception:
                batch = 0
            # ★ 只中止**某一条**任务（2026-09-28 用户要求）：界面上每个平台一行，
            #   某一条卡住时不该连累整批 —— 传 id 就只收这一条，待执行项原样保留。
            try:
                one_id = int(d.get("id") or 0)
            except Exception:
                one_id = 0
            if one_id:
                cur = self.store._rows("SELECT * FROM publishes WHERE id=?", (one_id,))
                cur = cur[0] if cur else None
                if cur and cur.get("status") == "pending":
                    # 还在排队：直接划掉就行，不用立旗
                    # （立了旗反而会在它将来被拾起来时**当场秒杀** —— 但用户此刻
                    #   的意图就是别发，所以划掉是准的；旗不能留，见 PUB_CANCEL_JOBS）
                    with self.store.lock:
                        with sqlite3.connect(self.store.db_path) as c:
                            c.execute("UPDATE publishes SET status='canceled', step='已中止',"
                                      " updated_at=? WHERE id=?",
                                      (time.strftime("%Y-%m-%d %H:%M:%S"), one_id))
                else:
                    PUB_CANCEL_JOBS.add(one_id)      # 正在跑：立旗，由引擎收掉
                row = self.store._rows("SELECT * FROM publishes WHERE id=?", (one_id,))
                return self._json({"ok": True, "jobs": row, "stopping": [one_id]})
            # ★ 先给「正在跑的那个」立旗，让 run_real_job 的等待循环把它收掉
            #   （`cancel_batch` 只管 pending，见 PUB_CANCEL_JOBS 的说明）。
            running = [r["id"] for r in self.store.get_batch(batch)
                       if r.get("status") == "running"]
            for jid in running:
                PUB_CANCEL_JOBS.add(jid)
            rows = self.store.cancel_batch(batch)
            return self._json({"ok": True, "jobs": rows, "stopping": running})
        if p == "/api/metrics/fetch":
            # 真实抓取要逐平台开浏览器，很慢 —— 放后台跑，接口立刻返回；
            # UI 轮询 /api/metrics 看 fetch.note 的进度。
            started = start_fetch(self.store)
            return self._json({"ok": True, "started": started,
                               "note": "已开始抓取" if started else "已有抓取在进行中",
                               "supported": list(DATA_WORKER_SUPPORTED)})
        if p == "/api/report/run-now":
            content = run_report(self.store)
            return self._json({"ok": True, "content": content})
        if p == "/api/videos/cover":
            q = urllib.parse.parse_qs(u.query)
            try:
                vid = int((q.get("id") or ["0"])[0])
            except Exception:
                vid = 0
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return self._json({"ok": False, "error": "空文件"}, 400)
            if length > MAX_COVER_BYTES:
                return self._json({"ok": False,
                                   "error": "封面图过大（限 %dMB）" % (MAX_COVER_BYTES // 1024 // 1024)}, 400)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(ctype, "jpg")
            # 封面也走上传并发槽与读超时（2026-10-06 复核）：它同样是"读网络那一段"
            if not UPLOAD_SLOTS.acquire(blocking=False):
                return self._json({"ok": False, "error": "同时上传数已达上限，请稍后重试"}, 429)
            try:
                try:
                    self.connection.settimeout(UPLOAD_TIMEOUT)
                except OSError:
                    pass
                v, err = self.store.set_cover(vid, self.rfile, length, ext)
            finally:
                UPLOAD_SLOTS.release()
            if err:
                return self._json({"ok": False, "error": err}, 400)
            return self._json({"ok": True, "video": v})
        return self._send(404, b"not found", "text/plain; charset=utf-8")

    def _file(self, path: Path):
        try:
            data = path.read_bytes()
        except OSError:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(path.suffix.lower(), "application/octet-stream")
        return self._send(200, data, ctype)

    def _serve_media(self, vid, kind):
        row = self.store.get(vid)
        if not row:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        if kind == "cover":
            if not row.get("cover"):
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            path = self.store.covers_dir / row["cover"]
        else:
            path = self.store.videos_dir / row["stored"]
        return self._stream_file(path)

    def _stream_file(self, path: Path):
        try:
            fsize = path.stat().st_size
        except OSError:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = {
            ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
            ".webm": "video/webm", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
        }.get(path.suffix.lower(), "application/octet-stream")
        start, end, code = 0, fsize - 1, 200
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            seg = rng[6:].split("-")
            try:
                if seg and seg[0]:
                    start = int(seg[0])
                if len(seg) > 1 and seg[1]:
                    end = min(int(seg[1]), fsize - 1)
            except ValueError:
                start, end = 0, fsize - 1
            if start > end or start >= fsize:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % fsize)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, fsize))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(256 * 1024, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (OSError, ConnectionError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8947)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=str(APP_DIR / "data"))
    ap.add_argument("--desktop", action="store_true", help="桌面启动器使用：单实例与本机登录")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--mock-login", default="")
    ap.add_argument("--notify-dry-run", action="store_true")
    ap.add_argument("--ai-mock", action="store_true")
    ap.add_argument("--set-password", default=None, metavar="口令")
    args = ap.parse_args()
    global CURRENT_PORT, MOCK_LOGIN_KEYS, NOTIFY_DRY, AI_MOCK
    global DESKTOP_MODE, DESKTOP_INSTANCE_ID, DESKTOP_DATA_ID
    DESKTOP_MODE = args.desktop
    CURRENT_PORT = args.port
    MOCK_LOGIN_KEYS = set(x for x in (args.mock_login or "").split(",") if x)
    NOTIFY_DRY, AI_MOCK = args.notify_dry_run, args.ai_mock
    loopback = str(args.host).strip() in ("127.0.0.1", "localhost", "::1")
    global SERVER_IS_LOOPBACK
    SERVER_IS_LOOPBACK = loopback      # 供 /api/config/save 判断"运行中能否清空口令"
    if args.desktop and not loopback:
        print("桌面版只允许本机访问，请使用 --host 127.0.0.1")
        return 2
    lock, httpd, metadata = None, None, None
    try:
        # Acquire ownership and bind BEFORE Store can recover running jobs or start threads.
        if args.desktop:
            sys.path.insert(0, str(APP_DIR.parent))
            from launcher.common import atomic_json, data_id, validate_data_path
            from launcher.locking import FileLock
            data_dir = validate_data_path(args.data_dir, APP_DIR.parent)
            data_dir.mkdir(parents=True, exist_ok=True)
            lock = FileLock(data_dir / ".desktop.instance.lock")
            if not lock.acquire():
                print("该数据目录已有服务运行，请使用启动工具打开现有实例", flush=True)
                return 3
            DESKTOP_INSTANCE_ID = os.environ.get("VP_INSTANCE_TOKEN") or secrets.token_hex(16)
            DESKTOP_DATA_ID = data_id(data_dir)
        else:
            data_dir = Path(args.data_dir)
        if args.set_password is None:
            try:
                httpd = ThreadingHTTPServer((args.host, args.port), Handler)
            except OSError as e:
                print("端口 %s 绑定失败：%s" % (args.port, e), flush=True)
                return 4
            CURRENT_PORT = httpd.server_address[1]
        store = Store(data_dir)
        Handler.store = store
        try:
            migrate_auth(store)      # 旧格式：口令从 config.json 迁到 auth.json（只做一次）
        except OSError as e:
            # ★ 失败关闭（2026-10-06 复核要求）：迁移写不进去就**别启动** ——
            #   吞掉错误继续的话，轻则口令没了（明文已被删），重则每次读配置都重试迁移。
            print("拒绝启动：旧格式口令迁移失败，未改动原数据。原因：%s" % e, flush=True)
            return 2
        load_config(store)           # ★ 启动时校验一次配置：损坏就地备份并置 CONFIG_STATE，
                                     #   否则要等到第一次读配置才被发现（健康检查会报假的"正常"）
        if args.set_password is not None:
            # ★ 这条路必须能在 auth.json 损坏时照常走 —— 它是唯一的命令行恢复入口
            auth = {"口令版本": 1, "更新时间": time.strftime("%Y-%m-%d %H:%M:%S")}
            set_password(auth, args.set_password)
            save_auth(store, auth)
            print("访问口令已设好：%s" % auth_path(store))
            return 0
        try:
            _auth = load_auth(store)
            _auth_broken = False
        except AuthBroken:
            _auth, _auth_broken = {}, True
        if not loopback and (_auth_broken or not (_auth.get("口令哈希") or "")):
            if _auth_broken:
                print("拒绝启动：口令文件 auth.json 已损坏。"
                      "请在数据目录删除该文件，再用 --set-password 重设", flush=True)
            else:
                print("拒绝启动：对外监听必须先设置访问口令；可用 --set-password 设置", flush=True)
            return 2
        with sqlite3.connect(store.db_path) as c:
            c.execute("UPDATE publishes SET status='fail', step='服务重启中断', error='服务重启导致中断，可一键重试', updated_at=? WHERE status='running'",
                      (time.strftime("%Y-%m-%d %H:%M:%S"),))
        if args.desktop:
            metadata = data_dir / ".desktop.instance.json"
            atomic_json(metadata, {"pid": os.getpid(), "port": CURRENT_PORT,
                                   "instance_id": DESKTOP_INSTANCE_ID, "data_id": DESKTOP_DATA_ID})
        threading.Thread(target=cleanup_stale_workers, args=(store,), daemon=True).start()
        threading.Thread(target=publish_engine, args=(store,), daemon=True).start()
        threading.Thread(target=report_scheduler, args=(store,), daemon=True).start()
        url = "http://%s:%d/" % ("127.0.0.1" if loopback else args.host, CURRENT_PORT)
        print("短视频发布工具(v%s) 已启动: %s" % (APP_VERSION, url), flush=True)
        print("数据目录: %s" % store.data_dir, flush=True)
        if not args.no_browser:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        def stop_signal(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop_signal)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("已停止")
        return 0
    except (OSError, ValueError) as e:
        print("启动失败：%s" % e, flush=True)
        return 1
    finally:
        if httpd:
            httpd.server_close()
        if lock and lock.file:
            try:
                kill_all_workers()
                if metadata:
                    metadata.unlink(missing_ok=True)
            finally:
                lock.release()


if __name__ == "__main__":
    sys.exit(main())

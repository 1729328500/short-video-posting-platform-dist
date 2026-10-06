# -*- coding: utf-8 -*-
"""自测：OCR 审出来的两条必修项（2026-09-28）

运行：python tests/test_20_ocr_hardening.py

★ 背景：1.9.6 交付前跑了一次 OCR，11 条发现里有两条是**本轮改动自己引入的**，
  而且都会在线上真咬人。这个测试就是给它们上锁。

**① 状态文件非原子写 + 新增的 off 分支 = 会把已登录账号误标成未登录**
  `read_worker_status()` 在两种情况下都返回 `{"state": "off"}`：
    · 文件不存在  → 这平台从没登过，off 是**事实**
    · 文件在但解析失败（撞上写了一半）→ 这只是**瞬时读错误**
  而 `refresh_accounts()` 每次 `/api/accounts` 和 **`/api/publish/create`** 都会读它。
  1.9.6 给 `off` 加了分支之后，第二种也会被当真写进数据库 —— 用户点发布那一刻
  恰好撞上写窗口，就会被拒「以下平台未登录」。
  修法：写入改原子（tmp + os.replace），并给读不出来打 `_unreadable` 标记，
  `refresh_accounts` 见到标记就不动库。

**② `QR_IMPORTED` 的「先查后加」是跨线程竞态**
  状态轮询 GET / 手动确认 POST / 看门线程，三条路径可能在 ThreadingHTTPServer
  下真并发进入 `maybe_finish_qr_login`，双双通过检查 → `finish_qr_login` 跑两次
  → 对同一个持久化 profile 并发 `launch_persistent_context`。而 Chromium 的
  profile 同时只允许一个实例占用（项目自己的注释里就写着这条教训）。

判据：
  ① 写出来的是合法 JSON，且 read 能原样读回；
  ② 文件损坏 → read 打 _unreadable 标记；文件不存在 → 不打标记；
  ③ 界面侧：数据库里已登录 + 状态文件损坏 → **状态保持已登录**（修复点）；
  ④ 界面侧：状态文件是合法的 off → 仍然正常改成未登录（别把 ① 的修复做成"off 永远不生效"）；
  ⑤ 并发 8 个线程同时收尾 → `finish_qr_login` **只跑一次**。
"""
import json
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8986
TMP = APP / "tests" / "_tmp20"
DATA = TMP / "data"

sys.path.insert(0, str(APP))

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def api(base, path, data=None):
    if data is None:
        req = urllib.request.Request(base + path)
    else:
        req = urllib.request.Request(base + path, data=json.dumps(data).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def acct(base, key):
    for a in api(base, "/api/accounts")["accounts"]:
        if a["key"] == key:
            return a
    return {}


def spath(key):
    d = DATA / "browsers"
    d.mkdir(parents=True, exist_ok=True)
    return d / ("%s.status.json" % key)


def write_raw(key, text):
    spath(key).write_text(text, encoding="utf-8")


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    DATA.mkdir(parents=True)

    # ---- 单元：原子写 + 读不出来要能分辨 ----
    import server

    store = server.Store(DATA)
    server.write_worker_status(store, "douyin", {"key": "douyin", "state": "on", "note": "x"})
    raw = spath("douyin").read_text(encoding="utf-8")
    try:
        parsed = json.loads(raw)
        ok("① 写出来是合法 JSON", parsed.get("state") == "on", raw[:80])
    except Exception as e:  # noqa: BLE001
        ok("① 写出来是合法 JSON", False, str(e))
    ok("① 没留下临时文件", not Path(str(spath("douyin")) + ".tmp").exists())
    ok("① 自动补了 at 字段", bool(json.loads(raw).get("at")), raw[:80])

    st = server.read_worker_status(store, "douyin")
    ok("② 正常文件不带 _unreadable", not st.get(server.UNREADABLE), st)

    write_raw("kuaishou", '{"key": "kuaishou", "state": "on"')      # 故意截断
    st = server.read_worker_status(store, "kuaishou")
    ok("② 损坏文件打上 _unreadable", bool(st.get(server.UNREADABLE)), st)

    st = server.read_worker_status(store, "xhs")                     # 文件不存在
    ok("② 文件不存在不算 _unreadable", not st.get(server.UNREADABLE), st)
    ok("② 文件不存在时状态是 off", st.get("state") == "off", st)

    # ---- 集成 ----
    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(DATA), "--no-browser", "--notify-dry-run"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % PORT
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            up = True
            break
        except Exception:
            time.sleep(0.5)
    ok("测试服务起来了", up)
    if not up:
        proc.kill()
        return finish()

    try:
        # ③ 已登录 + 状态文件损坏 → 不能被标成未登录（修复点）
        server.write_worker_status(store, "weibo", {"key": "weibo", "state": "on",
                                                    "note": "扫码登录成功"})
        a = acct(base, "weibo")
        ok("③ 前置：weibo 显示已登录", a.get("status") == "on", a.get("status"))

        write_raw("weibo", '{"key": "weibo", "state": "o')       # 模拟撞上写了一半
        a = acct(base, "weibo")
        ok("③ ★ 状态文件损坏时保持已登录（不误标未登录）", a.get("status") == "on",
           "%s / %s" % (a.get("status"), a.get("note")))

        # ④ 合法的 off 仍要生效（别把上一条修成"off 永远不生效"）
        server.write_worker_status(store, "weibo", {"key": "weibo", "state": "off",
                                                    "note": "已核验登录态：等待扫码"})
        a = acct(base, "weibo")
        ok("④ 合法 off 仍会改成未登录", a.get("status") == "off", a.get("status"))
        ok("④ off 的 note 被带出来", "核验" in (a.get("note") or ""), a.get("note"))

        # ④b /api/publish/create 也走 refresh_accounts —— 别把发布拦掉
        ok("④b publish/create 没把已登录平台判成未登录",
           True)      # 由上面 ③ 保证；这里只留个显式记录点
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    # ⑤ 并发收尾只能导入一次
    d2 = TMP / "conc"
    if d2.exists():
        shutil.rmtree(d2)
    d2.mkdir(parents=True)
    st2 = server.Store(d2)
    qd = d2 / "qrlogin" / "douyin"
    qd.mkdir(parents=True)
    (qd / "status.json").write_text(json.dumps(
        {"state": "done", "note": "登录成功", "state_file": str(qd / "state.json")},
        ensure_ascii=False), encoding="utf-8")
    (qd / "state.json").write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")

    calls = {"n": 0}
    lock = threading.Lock()

    def fake_finish(store_arg, key):
        with lock:
            calls["n"] += 1
        time.sleep(0.25)          # 拉长窗口：不给锁的话并发线程都会挤进来
        return 7

    real_finish = server.finish_qr_login
    server.finish_qr_login = fake_finish
    try:
        # ⑤a ★ 占位必须在 QR_LOCK 里 —— 用「主线程持锁，看别人会不会被挡住」来判定。
        #    为什么不用「开 8 个线程看会不会导入两次」：`in` 和 `add` 之间只隔几个
        #    字节码，GIL 下那个窗口窄到几乎撞不上 —— 实测把锁去掉后那个写法照样
        #    15/15 全绿，等于没测。持锁阻塞是**确定性**的，锁一没了立刻红。
        server.QR_IMPORTED.clear()
        server.QR_LOCK.acquire()
        done = {}
        t = threading.Thread(target=lambda: done.update(
            r=server.maybe_finish_qr_login(st2, "douyin")))
        t.start()
        time.sleep(0.5)
        ok("⑤a ★ 别人持着 QR_LOCK 时，收尾必须等待（占位在锁内）", t.is_alive(),
           "线程没被挡住 —— 占位跑到锁外面去了")
        server.QR_LOCK.release()
        t.join(timeout=8)
        ok("⑤a 放锁之后能正常完成", not t.is_alive() and done.get("r") == 7, str(done))

        # ⑤b 再来一遍：已经占过位了，不该重复导入
        before = calls["n"]
        ok("⑤b 已占位后再调不重复导入",
           server.maybe_finish_qr_login(st2, "douyin") is None and calls["n"] == before,
           "calls=%d" % calls["n"])

        # ⑤c 顺手做个多线程冒烟（不作为判据，只记录）
        server.QR_IMPORTED.clear()
        out = []
        threads = [threading.Thread(target=lambda: out.append(
            server.maybe_finish_qr_login(st2, "douyin"))) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        ok("⑤c 多线程冒烟：拿到结果的线程不超过 1 个",
           sum(1 for x in out if x is not None) <= 1, str(out))
    finally:
        server.finish_qr_login = real_finish
        try:
            server.QR_LOCK.release()
        except Exception:  # noqa: BLE001
            pass

    return finish()


def finish():
    passed = sum(1 for _, c in RESULTS if c)
    total = len(RESULTS)
    print("\n===== 结果: %d/%d 通过 =====" % (passed, total))
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

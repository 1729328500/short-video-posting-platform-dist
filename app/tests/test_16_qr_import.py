# -*- coding: utf-8 -*-
"""自测：登录成功后的「收尾导入」不能依赖前端轮询

运行：python tests/test_16_qr_import.py
依赖：Playwright + msedge（真开一次浏览器把 cookie 灌进 profile）

★ 为什么需要这个测试（2026-09-28 实测踩过）：
  可交互登录（noVNC 那条路）产出的是 storage_state JSON，而本项目自己的链路
  认的是**持久化 profile**，中间必须有一次「导入」。而导入原来只挂在
  `/api/accounts/qr/status` 这个 GET 里 —— **靠前端轮询触发**。于是：

    用户扫码成功 → worker 把状态写成 done、把 cookie 导出到 state.json
                 → 用户点了「我已登录完成」（走 /api/accounts/confirm）
                 → confirm **无条件**写「已登录」，却从不导入
                 → 账号页绿色 ✅，但 profile 目录压根不存在
                 → 发布时判定未登录 ❌

  实测微博/B站就是这样：cookie 抓到了 23/28 条躺在 qrlogin/<key>/state.json，
  但 /data/browsers/weibo 和 bilibili 目录不存在，界面却显示「已确认登录（手动）」。

判据：
  ① 状态为 done 时，`/api/accounts/confirm` 必须真的把 cookie 灌进 profile；
  ② `/api/accounts/qr/status` 这条老路径仍然有效；
  ③ 取消之后**不许**再导入（用户点了取消，不能被收尾逻辑覆盖）；
  ④ 没有 done 状态时，confirm 不能凭空造出 profile（别谎报成功）。
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))     # ⑤ 要在本进程内 import server 验幂等
PORT = 8982
TMP = APP / "tests" / "_tmp16"
DATA = TMP / "data"

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
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def qr_dir(key):
    d = DATA / "qrlogin" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_done(key, cookies=1):
    """造出「worker 判定登录成功」的现场：status.json=done + state.json 有 cookie"""
    d = qr_dir(key)
    sf = d / "state.json"
    sf.write_text(json.dumps({
        "cookies": [{"name": "t%d" % i, "value": "v%d" % i,
                     "domain": ".example.com", "path": "/"} for i in range(cookies)],
        "origins": [],
    }), encoding="utf-8")
    (d / "status.json").write_text(json.dumps(
        {"state": "done", "note": "登录成功", "url": "https://example.com/",
         "state_file": str(sf), "account": "u1", "account_name": "测试号"},
        ensure_ascii=False), encoding="utf-8")


def profile_dir(key):
    return DATA / "browsers" / key


def status_file(key):
    return DATA / "browsers" / ("%s.status.json" % key)


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    DATA.mkdir(parents=True)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
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
        # ① ★ 核心：「我已登录完成」必须顺带把 cookie 灌进 profile
        make_done("weibo", cookies=3)
        ok("①前置：weibo 还没有 profile", not profile_dir("weibo").exists())
        r = api(base, "/api/accounts/confirm", {"key": "weibo"})
        ok("① confirm 返回 ok", r.get("ok"), str(r)[:120])
        ok("① confirm 之后 profile 目录建出来了", profile_dir("weibo").exists(),
           str(profile_dir("weibo")))
        ok("① 导入条数正确（3 条）", r.get("imported") == 3, str(r.get("imported")))
        if status_file("weibo").exists():
            st = json.loads(status_file("weibo").read_text(encoding="utf-8"))
            ok("① 状态文件写成 on 且注明已导入",
               st.get("state") == "on" and "导入" in (st.get("detail") or ""), str(st)[:160])
        else:
            ok("① 状态文件写成 on 且注明已导入", False, "状态文件不存在")

        # ② 老路径（状态轮询）仍然有效
        make_done("bilibili", cookies=2)
        q = api(base, "/api/accounts/qr/status?key=bilibili")
        ok("② qr/status 仍能触发导入", q.get("status", {}).get("imported") == 2,
           str(q.get("status", {}).get("imported")))
        ok("② profile 目录建出来了", profile_dir("bilibili").exists())

        # ③ 取消之后不许再导入（用户点了取消，收尾逻辑不能覆盖它）
        make_done("kuaishou", cookies=5)
        api(base, "/api/accounts/qr/cancel", {"key": "kuaishou"})
        q = api(base, "/api/accounts/qr/status?key=kuaishou")
        ok("③ 取消后 qr/status 不再导入", q.get("status", {}).get("imported") is None,
           str(q.get("status", {}).get("imported")))
        ok("③ 取消后没有凭空建出 profile", not profile_dir("kuaishou").exists())

        # ④ 没有 done 状态 → confirm 不能谎报导入
        (qr_dir("toutiao") / "status.json").write_text(json.dumps(
            {"state": "waiting", "note": "等待扫码"}), encoding="utf-8")
        r = api(base, "/api/accounts/confirm", {"key": "toutiao"})
        ok("④ 无 done 时 imported 为空", r.get("imported") is None, str(r.get("imported")))
        ok("④ 无 done 时不建 profile", not profile_dir("toutiao").exists())
        # ★ 别直接 [0]（OCR 2026-09-30 指出）：账号行要是没登出来，这行会 IndexError
        #   把整轮跑挂掉 —— 那正好把这条判据要暴露的失败给盖住了。
        _m4 = [x for x in api(base, "/api/accounts")["accounts"] if x["key"] == "toutiao"]
        ok("④ 账号行存在（没被上一步弄丢）", bool(_m4), "accounts 里没有 toutiao")
        a = _m4[0] if _m4 else {}
        ok("④ 状态说明是「手动」而不是「扫码登录成功」", "手动" in (a.get("note") or ""),
           a.get("note"))

        # ══════════ ⑤ 重复点「登录」必须幂等（2026-09-30 实测事故）══════════
        # 事故：start_qr_login 一进门就 kill 掉上一个进程 —— 用户「登了没反应 → 再点一次」，
        # 正好把**正在盯着他扫码的那个进程**杀掉重开。实测视频号一天被点了 10 次
        # （抖音 10、快手 9）；而判登录要连续 2 轮（约 4 秒）才认，被杀掉的那次永远等不到。
        # 这里在**本进程内**验（不真起浏览器）：只注入一个假进程对象 + 状态文件。
        import server as srv

        class _FakeProc:
            def __init__(self, alive):
                self._alive = alive

            def poll(self):
                return None if self._alive else 0

        _st5 = srv.Store(DATA)
        _wk5 = qr_dir("weibo")
        _wk5.mkdir(parents=True, exist_ok=True)
        (_wk5 / "status.json").write_text(json.dumps({"state": "qr", "rounds": 3}),
                                          encoding="utf-8")
        srv.QR_WORKERS["weibo"] = _FakeProc(True)
        ok("⑤ 已在等 → 判定为「在等」", srv.qr_login_running(_st5, "weibo") is True)
        _r5 = srv.start_qr_login(_st5, "weibo")
        ok("⑤ 重复点「登录」不重开（幂等）", _r5.get("state") == "already", _r5)
        ok("⑤ 原有状态文件没被清掉（那个进程还在盯）", (_wk5 / "status.json").exists())
        srv.QR_WORKERS["weibo"] = _FakeProc(False)
        ok("⑤ 进程已退出 → 不算在等（可以重开）",
           srv.qr_login_running(_st5, "weibo") is False)
        (_wk5 / "status.json").write_text(json.dumps({"state": "done"}), encoding="utf-8")
        srv.QR_WORKERS["weibo"] = _FakeProc(True)
        ok("⑤ 已到终态(done) → 不算在等", srv.qr_login_running(_st5, "weibo") is False)

        # ⑥ 心跳：界面要能看出"它还在盯"（worker 写轮次/已等秒数，界面显示出来）
        _qw = (APP / "login_qr_worker.py").read_text(encoding="utf-8")
        _ui2 = (APP / "ui" / "app.js").read_text(encoding="utf-8")
        ok("⑥ worker 每轮写心跳（rounds / elapsed）",
           '"rounds": rounds' in _qw and '"elapsed"' in _qw)
        ok("⑥ 界面把心跳显示出来（第 N 轮 · 已等 M 秒 · 别重复点）",
           "qrAlive" in _ui2 and "轮 · 已等" in _ui2 and "不用重复点" in _ui2)
    finally:
        try:
            proc.kill()
        except Exception:
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

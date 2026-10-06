# -*- coding: utf-8 -*-
"""自测：发布队列要能被**主动中止**（不只是取消排队）

运行：python tests/test_22_publish_cancel.py

★ 为什么需要（2026-09-28 用户实测）：小红书账号被封，页面永远停在发布页不变，
  SAU 就一直「冲刺发布视频」空转 —— 而界面上唯一的按钮叫「取消未执行项」，
  它背后是：

      UPDATE publishes SET status='canceled' ... WHERE batch=? AND status='pending'
                                                              ^^^^^^^^^^^^^^^^^^ 只动待执行的

  **正在跑的那个根本不在取消范围内**，等待循环也不查取消信号。于是用户只能干等
  30 分钟超时（那还是 1.9.7 才加的兜底；再往前是永远转）。账号被封、验证码不来、
  平台页面卡死 —— 这几种情况下「中止」是刚需。

判据：
  ① wait_worker：进程自己退了 → "done"
  ② wait_worker：到 deadline 还没退 → "timeout"
  ③ wait_worker：should_stop 说了停 → "stopped"（且**不等**到 deadline）
  ④ 取消接口会报出它给哪些「正在跑的任务」立了中止旗
  ⑤ 待执行项仍然照旧被取消（别把老功能弄丢）
  ⑥ 中止旗是**按任务**立的 —— 重试同一条任务不能被自己的旧旗秒杀
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8987
TMP = APP / "tests" / "_tmp22"
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
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def unit_wait_worker():
    import server

    # ① 正常退出
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    ok("① 自己退了 → done", server.wait_worker(p, time.monotonic() + 10, poll=0.2) == "done")

    # ② 睡死 → 到点 timeout
    p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    t0 = time.time()
    r = server.wait_worker(p2, time.monotonic() + 2, poll=0.2)
    ok("② 睡死 → timeout", r == "timeout", r)
    ok("② 超时大约在上限处触发", 0.5 < time.time() - t0 < 5.0, "%.1fs" % (time.time() - t0))
    server.kill_worker(p2, grace=5)

    # ③ should_stop → stopped，且必须**提前**返回（不是等满 deadline）
    p3 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    t0 = time.time()
    r = server.wait_worker(p3, time.monotonic() + 30, poll=0.2, should_stop=lambda: True)
    dt = time.time() - t0
    ok("③ should_stop → stopped", r == "stopped", r)
    ok("③ 中止是立刻生效的（没等到 30 秒的上限）", dt < 3.0, "%.1fs" % dt)
    server.kill_worker(p3, grace=5)

    # ③b should_stop 一直为假时不能误报 stopped
    p4 = subprocess.Popen([sys.executable, "-c", "pass"])
    ok("③b should_stop 恒假时正常返回 done",
       server.wait_worker(p4, time.monotonic() + 10, poll=0.2, should_stop=lambda: False) == "done")


def integration():
    if TMP.exists():
        shutil.rmtree(TMP)
    DATA.mkdir(parents=True)
    (DATA / "config.json").write_text(json.dumps({"发布间隔": [3, 4]}), encoding="utf-8")

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
        return

    try:
        req = urllib.request.Request(
            base + "/api/videos/upload?name=" + urllib.parse.quote("中止测试.mp4"),
            data=b"\x00" * 4096, method="POST")
        vid = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())["video"]["id"]

        j = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["douyin", "channels", "kuaishou"],
            "mode": "now", "mock": True,
            "copies": {"common": {"title": "中止测试", "body": "", "topics": ""}}})
        b = j["batch"]
        ok("创建 3 条任务的批次", j.get("ok") and j.get("count") == 3, str(j)[:100])

        # 等引擎把第一条拾起来（mock 跑得快，可能立刻就跑完了）
        running_id = None
        end = time.time() + 25
        while time.time() < end:
            jobs = api(base, "/api/publish/batch?id=%d" % b)["jobs"]
            run = [r for r in jobs if r["status"] == "running"]
            if run:
                running_id = run[0]["id"]
                break
            if all(r["status"] in ("success", "fail", "canceled") for r in jobs):
                break
            time.sleep(0.3)

        # ④⑤ 直接取消整个批次
        r = api(base, "/api/publish/cancel", {"batch": b})
        ok("④ 取消接口返回了 stopping 字段", "stopping" in r, str(r)[:120])

        jobs = api(base, "/api/publish/batch?id=%d" % b)["jobs"]
        pend = [x for x in jobs if x["status"] == "pending"]
        ok("⑤ 待执行项都被取消了", not pend, [(x["platform"], x["status"]) for x in jobs])

        # ④b 人为把一个任务置成 running，再取消，接口应该把它报进 stopping
        jid = jobs[-1]["id"]
        with sqlite3.connect(DATA / "app.db") as c:
            c.execute("UPDATE publishes SET status='running', step='人为置为执行中' WHERE id=?", (jid,))
        r = api(base, "/api/publish/cancel", {"batch": b})
        ok("④b 正在跑的任务被点名立旗", jid in (r.get("stopping") or []),
           "stopping=%s 期望含 %d" % (r.get("stopping"), jid))

        # ⑥ 重试同一条任务：不能被自己的旧旗秒杀
        #    （run_real_job 开工第一件事就是 discard 自己的旗）
        with sqlite3.connect(DATA / "app.db") as c:
            c.execute("UPDATE publishes SET status='fail', step='失败' WHERE id=?", (jid,))
        rt = api(base, "/api/publish/retry", {"id": jid})
        ok("⑥ 重试被接受", rt.get("ok"), str(rt)[:120])
        time.sleep(6)
        after = api(base, "/api/publish/batch?id=%d" % b)["jobs"]
        row = [x for x in after if x["id"] == jid][0]
        ok("⑥ ★ 重试没有被旧的中止旗秒杀", row["status"] != "canceled" or "中止" not in (row.get("error") or ""),
           "%s / %s" % (row["status"], row.get("error")))

        # ⑧ ★ 只中止某一条：别的平台不受影响（用户 2026-09-28 明确要求）
        j2 = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["douyin", "channels", "kuaishou", "xhs"],
            "mode": "now", "mock": True,
            "copies": {"common": {"title": "单条中止", "body": "", "topics": ""}}})
        b2 = j2["batch"]
        jobs2 = api(base, "/api/publish/batch?id=%d" % b2)["jobs"]
        target = jobs2[-1]["id"]          # 最后一条（排在队尾，肯定还没跑）
        others = [x["id"] for x in jobs2 if x["id"] != target]

        r = api(base, "/api/publish/cancel", {"batch": b2, "id": target})
        ok("⑧ 单条中止返回 stopping", target in (r.get("stopping") or []), str(r)[:120])

        after = api(base, "/api/publish/batch?id=%d" % b2)["jobs"]
        trow = [x for x in after if x["id"] == target][0]
        ok("⑧ 那一条已中止", trow["status"] == "canceled", trow["status"])
        # 其余任务不该被顺手划掉（mock 跑得快，允许它们已 success/fail，但不能是 canceled）
        rest = [x for x in after if x["id"] in others]
        ok("⑧ ★ 其余平台没有被连累",
           all(x["status"] != "canceled" for x in rest),
           [(x["platform"], x["status"]) for x in rest])
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def unit_browser_gone():
    """⑦ 关窗检测：靠发布日志里的「页面已关闭」信号"""
    import server

    d = TMP / "logtest"
    d.mkdir(parents=True, exist_ok=True)
    sf = d / "job_99.json"
    log = d / "job_99.sau.log"

    ok("⑦ 日志不存在时不误判", server.browser_closed_by_human(sf) is False)

    log.write_text("[sau 12:00:00 INFO] 🏃 视频正在发布中...\n", encoding="utf-8")
    ok("⑦ 正常日志不判为关窗", server.browser_closed_by_human(sf) is False)

    log.write_text(
        "[sau 12:00:01 INFO] 🏃 小人正在冲刺发布视频\n"
        "[sau 12:00:13 WARN] 😵 发布仍未完成(第3次)，异常: Target page, context or "
        "browser has been closed\n", encoding="utf-8")
    ok("⑦ ★ 日志出现「页面已关闭」→ 判为人工关窗",
       server.browser_closed_by_human(sf) is True)

    # 只看尾部：老早以前的关窗记录不该一直触发
    log.write_text("[sau 12:00:00 WARN] Target closed\n" + ("x" * 6000) + "\n",
                   encoding="utf-8")
    ok("⑦ 只读尾部：很久以前的信号不再触发",
       server.browser_closed_by_human(sf) is False)


def main():
    unit_wait_worker()
    unit_browser_gone()
    integration()
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

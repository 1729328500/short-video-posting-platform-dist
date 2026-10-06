# -*- coding: utf-8 -*-
"""自测：发布队列「等待发布间隔」要看得见 + 发布进程等待要有超时兜底

运行：python tests/test_18_publish_wait.py

★ 为什么需要这个测试（2026-09-28 用户反馈）：
  「批量发布时第一个发布完成后，第二个一直在排队」—— 查引擎日志发现不是卡死：

      [13:36:04] finish job #3 -> success
      [13:36:04] interval sleep 82.2s      ← 这 82 秒里第二个任务就是「排队中」
      [13:37:28] pick job #4 [channels]

  引擎在每两个任务之间会按「发布间隔」（默认 60~180 秒随机）睡一段，而界面上下一条
  任务只有「排队中」三个字，分不清是**按设计在等**还是**卡死了**。

  同时确实存在一条会真卡死的路径：run_real_job 里等发布进程退出的是
  `while p.poll() is None:`，**没有上限** —— 进程一旦挂住，这个批次后面所有任务
  就永远停在「排队中」。

判据：
  ① 等待期间接口必须报出 engine_wait（秒数、剩余），不是空；
  ② 剩余秒数在往下走；
  ③ 界面上把「排队中」显示成带倒计时的样子；
  ④ wait_worker 到点返回 "timeout"，正常退出返回 "done"，被叫停返回 "stopped"；
  ⑤ 超时后 kill_worker 能把进程收干净。
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8984
TMP = APP / "tests" / "_tmp18"
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


def unit_wait_worker():
    """④⑤ 直接单测等待/超时函数 —— 拿一个真会睡死的小进程来验"""
    import server

    # 正常退出 → True
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    # ★ 1.9.9 起 wait_worker 返回的是**原因字符串**（"done"/"timeout"/"stopped"），
    #   不再是布尔 —— 因为「超时」和「用户点了中止」要给出不同的状态与文案。
    ok("④ 进程正常退出 → wait_worker 返回 done",
       server.wait_worker(p, time.monotonic() + 10, poll=0.2) == "done")

    # 睡死 → 到点返回 timeout
    p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    t0 = time.time()
    got = server.wait_worker(p2, time.monotonic() + 2, poll=0.2)
    ok("④ 进程睡死 → 到点返回 timeout", got == "timeout", got)
    dt = time.time() - t0
    ok("④ 超时大约在上限处触发（0.5~5 秒）", 0.5 < dt < 5.0, "%.1fs" % dt)

    # ⑤ 超时后用 kill_worker 收干净
    ok("⑤ 超时后进程还活着（需要收尾）", p2.poll() is None)
    server.kill_worker(p2, grace=5)
    time.sleep(0.5)
    ok("⑤ kill_worker 之后进程已退出", p2.poll() is not None, p2.poll())

    # tick 回调确实被调到了
    n = {"c": 0}
    p3 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.8)"])
    server.wait_worker(p3, time.monotonic() + 10, on_tick=lambda: n.__setitem__("c", n["c"] + 1), poll=0.1)
    ok("④ on_tick 回调有被调用", n["c"] > 0, n["c"])


def integration():
    if TMP.exists():
        shutil.rmtree(TMP)
    DATA.mkdir(parents=True)
    # 把发布间隔压到几秒，好在测试里观察到等待窗口（默认 60~180 秒，那样太慢）
    (DATA / "config.json").write_text(json.dumps({"发布间隔": [3, 4]}), encoding="utf-8")

    logf = open(TMP / "server.out.txt", "wb")
    # -u：不缓冲。否则服务一直跑，日志就一直是 0 字节，起不来时无从查起。
    proc = subprocess.Popen(
        [sys.executable, "-u", str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(DATA), "--no-browser", "--notify-dry-run"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % PORT
    up = False
    lasterr = None
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            up = True
            break
        except Exception as e:
            lasterr = "%s: %s" % (type(e).__name__, e)
            time.sleep(0.5)
    if not up:
        print("  最后一次连接错误: %s" % lasterr)
    ok("测试服务起来了", up)
    if not up:
        # 起不来时把服务端日志和进程状态抖出来，别只报一句失败
        try:
            logf.close()
        except Exception:
            pass
        try:
            print("  服务端日志:\n%s" % (TMP / "server.out.txt").read_text(
                encoding="utf-8", errors="replace")[:800])
        except Exception:
            pass
        print("  进程状态: poll=%s" % proc.poll())
        proc.kill()
        return

    try:
        # 先真上传一个素材（/api/publish/create 要的是库里的 video_id，不是文件）
        req = urllib.request.Request(
            base + "/api/videos/upload?name=" + urllib.parse.quote("等待测试.mp4"),
            data=b"\x00" * 4096, method="POST")
        vid = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())["video"]["id"]

        # 造 3 条 mock 任务，队列里就会先跑一条、再等一段
        j = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["douyin", "channels", "bilibili"],
            "mode": "now", "mock": True,
            "copies": {"common": {"title": "等待测试", "body": "", "topics": ""}}})
        ok("创建 3 条任务的批次", j.get("ok") and j.get("count") == 3, str(j)[:120])
        b = j.get("batch")

        # ① 抓等待窗口：轮询直到看到 engine_wait
        seen = None
        samples = []
        end = time.time() + 40
        while time.time() < end:
            jj = api(base, "/api/publish/batch?id=%d" % b)
            w = jj.get("engine_wait")
            if w:
                seen = w
                samples.append(w.get("remaining"))
                if len(samples) >= 3:
                    break
            time.sleep(1)
        ok("① 等待期间接口报出 engine_wait", seen is not None, "没抓到（可能间隔太短）")
        ok("① engine_wait 带了剩余秒数", bool(seen) and isinstance(seen.get("remaining"), int),
           str(seen))
        ok("① engine_wait 带了本次间隔总长", bool(seen) and seen.get("seconds", 0) >= 1, str(seen))
        ok("② 剩余秒数在往下走", len(samples) >= 2 and samples[0] > samples[-1], str(samples))

        # 等批次跑完（3 条任务 + 2 段间隔）
        end = time.time() + 60
        while time.time() < end:
            jj = api(base, "/api/publish/batch?id=%d" % b)
            if all(r["status"] in ("success", "fail", "canceled", "manual")
                   for r in jj.get("jobs", [])):
                break
            time.sleep(1)
        jj = api(base, "/api/publish/batch?id=%d" % b)
        ok("批次最终跑完（等待不会卡住队列）",
           all(r["status"] == "success" for r in jj["jobs"]),
           [(r["platform"], r["status"]) for r in jj["jobs"]])
        ok("跑完之后 engine_wait 归位为 None", jj.get("engine_wait") is None, str(jj.get("engine_wait")))

        # ③ 界面把倒计时显示出来
        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/#publish")
            pg.wait_for_timeout(1000)
            # 走真实的界面发布流程（照 test_06）：选素材 → 勾平台 → 起批次 → 确认
            pg.select_option("#videoSel", str(vid))
            pg.wait_for_timeout(400)
            pg.check('.pubplat[data-k="douyin"]')
            pg.check('.pubplat[data-k="channels"]')
            pg.click("#pubStartBtn")
            pg.wait_for_timeout(600)
            pg.locator("#mFoot button").filter(has_text="确认发布").click()
            pg.wait_for_timeout(800)
            ok("③ 进度卡出现", pg.is_visible("#pubProgressCard"))
            # 第一条跑完后会进入发布间隔（测试里压到 3~4 秒），趁机抓界面文字
            found = ""
            for _ in range(60):
                txt = pg.eval_on_selector("#pubProgress", "e=>e.innerText")
                if "发布间隔" in txt:
                    found = txt
                    break
                time.sleep(0.4)
            ok("③ 界面显示「等发布间隔」倒计时", "发布间隔" in found,
               (found or pg.eval_on_selector("#pubProgress", "e=>e.innerText")).replace("\n", " / ")[:160])
            ok("③ 界面无 JS 报错", not errs, errs)
            br.close()
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    unit_wait_worker()
    integration()
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

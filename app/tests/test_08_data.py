# -*- coding: utf-8 -*-
"""v1.5 自测：数据页 —— 真实抓取（**不再有模拟数据**）

运行：python tests/test_08_data.py

★★ 本测试相比旧版的根本变化：
   旧版 run_fetch() 用 md5 造播放/点赞/评论，测试断言的是「模拟数据有数字」。
   那套已废弃 —— 编造的数字会被展示和推送，属于最不该有的行为。
   现在：接入真实抓取的平台抓真数据；**没接入的如实显示「未接入」，
   绝不会用编造数字顶替**。所以本测试断言的是「诚实性」，而不是「有数字」。

真实抓取需要登录态 + 网络，不适合放在无人值守的自测里，故用 `--no-real-fetch`
思路验证：确认「未登录时如实报错」「未接入的平台如实标注」「接口不再产生 mock 行」。
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8962
TMP = APP / "tests" / "_tmp"
SHOTS = APP / "tests" / "shots"

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


def main():
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    (TMP / "data").mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(TMP / "data"), "--no-browser", "--notify-dry-run"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % PORT
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            up = True
            break
        except Exception:
            time.sleep(0.25)
    ok("服务启动", up)
    if not up:
        proc.terminate()
        return finish()

    try:
        # ── ① 未抓取时：接口干净，没有任何编造数据 ──
        m0 = api(base, "/api/metrics")
        ok("初始看板为空（不预置任何数字）", m0["platforms"] == [] and m0["total"]["views"] == 0, m0["total"])
        # ★ 2026-09-29 改成**从 SUPPORTED 派生**：原来写死 ["douyin"]，
        #   于是每接入一个平台这条就假失败一次（判据测的是"如实标注"，
        #   不是"永远只有抖音"—— SUPPORTED 才是唯一事实来源）。
        sys.path.insert(0, str(APP))       # 本测试原本不导 app 侧模块，这里补一次
        import data_worker as _dw
        ok("接口标明哪些平台已接入真实抓取",
           sorted(m0.get("supported") or []) == sorted(_dw.SUPPORTED), m0.get("supported"))

        # ── ② 触发抓取：未登录时必须如实报错，且不产生任何数据行 ──
        r = api(base, "/api/metrics/fetch", {})
        ok("抓取接口是异步的（立刻返回）", r.get("ok") and "started" in r, r)
        done = False
        for _ in range(40):
            time.sleep(1)
            st = api(base, "/api/metrics").get("fetch") or {}
            if not st.get("running"):
                done = True
                break
        ok("抓取会结束（不会一直挂着）", done)

        rows = api(base, "/api/metrics")["platforms"]
        ok("未登录 → 不产生任何数据（不拿假数字凑）", rows == [], rows)

        # ── ③ 数据库里绝不能有 mock 来源的行 ──
        with sqlite3.connect(TMP / "data" / "app.db") as c:
            n_mock = c.execute("SELECT COUNT(*) FROM metrics WHERE source IS NULL OR source='mock'").fetchone()[0]
            n_all = c.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        ok("库里没有任何 mock 来源的数据行", n_mock == 0, "mock=%d 全部=%d" % (n_mock, n_all))

        # ── ④ 播报：没有真实数据时如实说明，不编造 ──
        rep = api(base, "/api/report/run-now", {})   # 传 {} 才会走 POST（该接口只收 POST）
        content = rep.get("content", "")
        ok("播报接口可用", rep.get("ok") and "数据播报" in content, content[:80])

        # ── ⑤ 数据表结构已扩展（真实抓取带来的账号级字段）──
        with sqlite3.connect(TMP / "data" / "app.db") as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(metrics)").fetchall()]
        for col in ("followers", "total_likes", "works_count", "nick_name"):
            ok("metrics 表有 %s 列" % col, col in cols, cols)

        # ── ⑥ UI ──
        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/#data")
            pg.wait_for_timeout(1000)
            ok("数据页可见", pg.is_visible("#page-data"))
            ok("8 张平台卡", pg.eval_on_selector_all("#dataGrid .gcard", "e=>e.length") == 8)
            body = pg.eval_on_selector("#dataGrid", "e=>e.innerText")
            # ★ 同上：从 SUPPORTED 派生（8 = 平台总数，上一行刚断言过）
            ok("未接入的平台如实标注",
               body.count("未接入真实抓取") == 8 - len(_dw.SUPPORTED), body.count("未接入真实抓取"))
            ok("已接入但未抓取的平台提示去抓取", "暂无数据" in body, body[:120])
            hint = pg.text_content("#dataHint")
            ok("汇总区不再声称是模拟抓取", "模拟" not in (hint or ""), hint)
            ok("按钮不再谎称「测试」", "测试" not in pg.text_content("#dataReportBtn"))
            pg.screenshot(path=str(SHOTS / "27-数据页.png"))
            ok("无 JS 报错", not errs, errs)
            br.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    return finish()


def finish():
    passed = sum(1 for _, c in RESULTS if c)
    total = len(RESULTS)
    print("\n===== 结果: %d/%d 通过 =====" % (passed, total))
    sys.exit(0 if passed == total and total > 0 else 1)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()

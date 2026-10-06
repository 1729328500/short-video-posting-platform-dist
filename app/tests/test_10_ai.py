# -*- coding: utf-8 -*-
"""v1.1 自测：AI 文案生成（mock 接口 / UI 全流程 / 停用回退 / 设置页）

运行：python tests/test_10_ai.py
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8965
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
        req = urllib.request.Request(base + path, data=json.dumps(data).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "data").mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(TMP / "data"), "--no-browser", "--notify-dry-run", "--ai-mock"],
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
        # ---- 接口：AI 状态 ----
        s = api(base, "/api/ai/status")
        ok("AI 状态接口（mock）", s.get("ok") is True and s.get("mock") is True, s)

        # ---- 接口：mock 生成（各平台不同） ----
        g = api(base, "/api/ai/generate", {"title": "通用标题", "body": "通用正文", "topics": "装修 防水"})
        copies = g.get("copies") or {}
        ok("生成返回 8 平台", g.get("ok") is True and len(copies) == 8, list(copies.keys()))
        titles = set(copies[k]["title"] for k in copies)
        ok("各平台标题互不相同", len(titles) == 8, titles)
        ok("平台内容含平台名",
           "抖音" in copies["douyin"]["title"] and "小红书" in copies["xhs"]["title"],
           (copies["douyin"]["title"], copies["xhs"]["title"]))

        # ---- 接口：停用 → 拒绝生成 ----
        api(base, "/api/ai/save", {"enabled": False})
        g2 = api(base, "/api/ai/generate", {"title": "T", "body": "B"})
        ok("停用后拒绝生成", g2.get("ok") is False and "停用" in g2.get("error", ""), g2)
        api(base, "/api/ai/save", {"enabled": True})

        # ---- UI 全流程（mock 生成） ----
        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/#publish")
            pg.wait_for_timeout(900)
            pg.fill("#fTitle", "测试通用标题")
            pg.fill("#fBody", "测试通用正文")
            pg.fill("#fTopics", "装修 防水")
            pg.wait_for_timeout(300)
            pg.click("#genAllBtn")
            pg.wait_for_timeout(1800)
            # 切抖音 / 小红书 查看
            pg.click('.tab[data-k="douyin"]')
            pg.wait_for_timeout(300)
            t_dy = pg.input_value("#fTitle")
            pg.click('.tab[data-k="xhs"]')
            pg.wait_for_timeout(300)
            t_xhs = pg.input_value("#fTitle")
            ok("UI：抖音版已填入（含抖音）", "抖音" in t_dy, t_dy)
            ok("UI：小红书版不同", "小红书" in t_xhs and t_xhs != t_dy, t_xhs)
            has = pg.eval_on_selector_all('.tab.has', "els=>els.map(e=>e.dataset.k)")
            ok("UI：8 平台带已生成标记", len(has) == 8, has)
            pg.screenshot(path=str(SHOTS / "28-AI生成.png"))

            # ---- 停用 AI → 点生成 → 回退基础版 ----
            api(base, "/api/ai/save", {"enabled": False})
            pg.click('.tab[data-k="common"]')
            pg.wait_for_timeout(200)
            pg.fill("#fTitle", "回退测试标题")
            pg.wait_for_timeout(200)
            pg.click("#genAllBtn")
            pg.wait_for_timeout(1500)
            pg.click('.tab[data-k="douyin"]')
            pg.wait_for_timeout(250)
            r1 = pg.input_value("#fTitle")
            pg.click('.tab[data-k="xhs"]')
            pg.wait_for_timeout(250)
            r2 = pg.input_value("#fTitle")
            ok("停用后回退基础版（各平台同文案）",
               r1.strip() == "回退测试标题" and r2.strip() == "回退测试标题", (r1, r2))
            api(base, "/api/ai/save", {"enabled": True})

            # ---- 设置页：AI 卡片 ----
            pg.goto(base + "/#settings")
            pg.wait_for_timeout(900)
            st = pg.text_content("#aiState")
            ok("设置页显示 AI 状态（模拟）", bool(st) and "模拟" in st, st)
            ok("设置页 AI 开关勾选", pg.is_checked("#aiEnabled") is True)

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
    main()

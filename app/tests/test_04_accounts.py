# -*- coding: utf-8 -*-
"""开发项 S5 自测：账号页 + 登录窗管理（模拟登录全链路）

运行：python tests/test_04_accounts.py
说明：--mock-login douyin 让抖音走本地模拟登录页（2 秒后自动“登录”），
      全链路可自测：打开登录窗 → 检测登录 → 自动关闭窗口 → 保留已登录状态。
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8952
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
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "data").mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(TMP / "data"), "--no-browser", "--mock-login", "douyin"],
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
        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/#accounts")
            pg.wait_for_timeout(900)

            ok("账号页可见", pg.is_visible("#page-accounts"))
            cnt = pg.eval_on_selector_all(".acard", "els=>els.length")
            ok("8 个平台卡片", cnt == 8, cnt)
            body = pg.eval_on_selector("#acGrid", "e=>e.innerText")
            ok("初始均为未登录", body.count("未登录") == 8, body.count("未登录"))
            pg.screenshot(path=str(SHOTS / "20-账号页-初始.png"))

            # 点【扫码登录】（抖音走 mock）→ 等待自动变为已登录
            pg.locator('.acard[data-key="douyin"]').locator(".act-login").click()
            on = False
            for _ in range(60):
                time.sleep(0.5)
                t = pg.locator('.acard[data-key="douyin"]').inner_text()
                if pg.locator('.acard[data-key="douyin"] .badge.ok').count():
                    on = True
                    break
            ok("模拟扫码后显示已登录", on, pg.locator('.acard[data-key="douyin"]').inner_text())
            pg.screenshot(path=str(SHOTS / "21-账号页-已登录.png"))
            # ★ 从 7 改成 6：西瓜视频与抖音**共用登录后台**（实测 studio.ixigua.com
            #   302 到 creator.douyin.com），抖音登录后西瓜跟着变已登录 —— 这是有意行为。
            cnt = pg.eval_on_selector_all("#acGrid .badge.info", "els=>els.length")
            ok("其余 6 个独立平台仍未登录（西瓜与抖音共用登录态，跟着变绿）", cnt == 6, cnt)
            xg = pg.locator('.acard[data-key="xigua"]').inner_text()
            ok("西瓜卡片显示与抖音共用登录态", "共用" in xg, xg.replace("\n", "|")[:70])

            # 关闭已经完成的登录窗口，保留登录态
            pg.locator('.acard[data-key="douyin"]').locator(".act-close").click()
            off = False
            for _ in range(20):
                time.sleep(0.5)
                t = pg.locator('.acard[data-key="douyin"]').inner_text()
                if pg.locator('.acard[data-key="douyin"] .badge.ok').count():
                    off = True
                    break
            ok("关闭窗口后保留登录状态", off, pg.locator('.acard[data-key="douyin"]').inner_text())

            acc = api(base, "/api/accounts")
            dy = [a for a in acc["accounts"] if a["key"] == "douyin"][0]
            ok("API 状态同步（on）", dy["status"] == "on", dy)
            ok("无 JS 报错", not errs, errs)
            br.close()
    finally:
        try:
            api(base, "/api/accounts/close", {"key": "douyin"})
        except Exception:
            pass
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

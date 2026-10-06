# -*- coding: utf-8 -*-
"""开发项 S1+S2 自测：应用骨架 + 素材库（上传/列表/搜索/重命名/删除/边界）

运行：python tests/test_01_materials.py
（需本机 Playwright + msedge；自动起服务、跑完关服务）
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8948
TMP = APP / "tests" / "_tmp"
SHOTS = APP / "tests" / "shots"

sys.path.insert(0, str(APP / "tests"))
from gen_mp4 import build_mp4  # noqa: E402

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "data").mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    vdir = TMP / "in"
    vdir.mkdir()
    a = vdir / "测试视频A.mp4"
    b = vdir / "测试视频B.mp4"
    build_mp4(a, seconds=12, width=1080, height=1920)
    build_mp4(b, seconds=75, width=720, height=1280)
    bad = vdir / "说明文档.txt"
    bad.write_text("不是视频", encoding="utf-8")
    vid_dir = TMP / "data" / "videos"

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(TMP / "data"), "--no-browser"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT,
    )
    base = "http://127.0.0.1:%d" % PORT
    up = False
    for _ in range(40):
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=1) as r:
                if json.loads(r.read().decode("utf-8"))["ok"]:
                    up = True
                    break
        except Exception:
            time.sleep(0.25)
    ok("服务启动 /api/health", up)
    if not up:
        proc.terminate()
        out, _ = proc.communicate(timeout=5)
        print(out.decode("utf-8", "ignore"))
        return finish()

    from playwright.sync_api import sync_playwright

    errs = []
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/")
            pg.wait_for_timeout(600)

            ok("页面标题", "短视频发布台" in pg.title())
            ok("导航 6 项", len(pg.query_selector_all(".nav-item")) == 6)
            ok("默认落在素材库", pg.is_visible("#page-library"))
            ok("空态提示可见", pg.is_visible("#vempty"))
            ok("无横向溢出", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))

            # 1) 多选上传两个视频
            pg.set_input_files("#fileInput", [str(a), str(b)])
            pg.wait_for_function("document.querySelectorAll('.vcard').length === 2", timeout=8000)
            names = pg.eval_on_selector_all(".vname", "els=>els.map(e=>e.textContent)")
            ok("上传后列表 2 条", len(names) == 2, names)
            metas = pg.eval_on_selector_all(".vmeta", "els=>els.map(e=>e.textContent)")
            ok("时长解析 12 秒", any("12 秒" in m for m in metas), metas)
            ok("时长解析 1 分 15 秒", any("1 分 15 秒" in m for m in metas), metas)
            ok("分辨率解析 1080×1920", any("1080×1920" in m for m in metas), metas)
            ok("大小/时间列存在", all(("MB" in m or "KB" in m or "B" in m) for m in metas), metas)
            pg.screenshot(path=str(SHOTS / "01-素材库-上传后.png"))

            # 2) 非法文件类型拦截
            pg.set_input_files("#fileInput", [str(bad)])
            pg.wait_for_timeout(700)
            ok("非法类型被拦截", pg.eval_on_selector_all(".vcard", "els=>els.length") == 2)

            # 3) 刷新持久化
            pg.reload()
            pg.wait_for_timeout(700)
            ok("刷新后仍在（持久化）", pg.eval_on_selector_all(".vcard", "els=>els.length") == 2)

            # 4) 搜索过滤
            pg.fill("#searchBox", "视频B")
            pg.wait_for_timeout(250)
            ok("搜索过滤", pg.eval_on_selector_all(".vcard", "els=>els.length") == 1)
            pg.fill("#searchBox", "")
            pg.wait_for_timeout(250)

            # 5) 重命名（第一张卡：测试视频B → 防水教程-01.mp4）
            pg.locator(".vcard").first.locator(".act-rename").click()
            pg.wait_for_timeout(200)
            inp = pg.locator(".vname-input").first
            inp.fill("防水教程-01.mp4")
            inp.press("Enter")
            pg.wait_for_function("document.querySelectorAll('.vname-input').length === 0", timeout=5000)
            pg.wait_for_timeout(400)
            names = pg.eval_on_selector_all(".vname", "els=>els.map(e=>e.textContent)")
            ok("重命名生效", "防水教程-01.mp4" in names, names)
            disk = sorted(x.name for x in vid_dir.iterdir())
            ok("磁盘文件同步改名", "防水教程-01.mp4" in disk, disk)
            pg.screenshot(path=str(SHOTS / "02-素材库-重命名后.png"))

            # 6) 重名上传 → 自动加 (1)
            pg.set_input_files("#fileInput", [str(a)])
            pg.wait_for_function("document.querySelectorAll('.vcard').length === 3", timeout=8000)
            names = pg.eval_on_selector_all(".vname", "els=>els.map(e=>e.textContent)")
            ok("重名自动加 (1)", "测试视频A(1).mp4" in names, names)

            # 7) 删除
            pg.locator(".vcard").filter(has_text="测试视频A(1)").locator(".act-del").click()
            pg.wait_for_function("document.querySelectorAll('.vcard').length === 2", timeout=5000)
            pg.wait_for_timeout(300)
            disk = sorted(x.name for x in vid_dir.iterdir())
            ok("删除后磁盘文件移除", "测试视频A(1).mp4" not in disk, disk)
            ok("删除后列表 2 条", pg.eval_on_selector_all(".vcard", "els=>els.length") == 2)

            # 8) 无扩展名重命名 → 自动补 .mp4
            pg.locator(".vcard").filter(has_text="测试视频A.mp4").locator(".act-rename").click()
            pg.wait_for_timeout(200)
            pg.locator(".vname-input").first.fill("防水教程-02")
            pg.locator(".vname-input").first.press("Enter")
            pg.wait_for_timeout(600)
            names = pg.eval_on_selector_all(".vname", "els=>els.map(e=>e.textContent)")
            ok("无扩展名自动补 .mp4", "防水教程-02.mp4" in names, names)

            pg.screenshot(path=str(SHOTS / "03-素材库-最终.png"))
            ok("无 JS 报错", not errs, errs)
            br.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
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

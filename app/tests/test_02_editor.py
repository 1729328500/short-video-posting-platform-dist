# -*- coding: utf-8 -*-
"""开发项 S3 自测：发布台 · 文案编辑（8 平台 Tab / 一键生成 / 草稿保存与恢复）

运行：python tests/test_02_editor.py
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
PORT = 8949
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
    a = TMP / "in" / "测试视频A.mp4"
    a.parent.mkdir(parents=True, exist_ok=True)
    build_mp4(a, seconds=12, width=1080, height=1920)
    b = TMP / "in" / "测试视频B.mp4"
    build_mp4(b, seconds=8, width=720, height=1280)

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
    ok("服务启动", up)
    if not up:
        proc.terminate()
        return finish()

    try:
        # 通过 API 先放入一个素材
        body = a.read_bytes()
        req = urllib.request.Request(
            base + "/api/videos/upload?name=" + urllib.parse.quote("测试视频A.mp4"),
            data=body, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read().decode("utf-8"))
        vid = j["video"]["id"]

        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/#publish")
            pg.wait_for_timeout(900)

            ok("发布台可见", pg.is_visible("#page-publish"))
            ok("无横向溢出", pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
            ok("文案 Tab 共 9 个", pg.eval_on_selector_all("#copyTabs .tab", "els=>els.length") == 9)

            # 从本地上传视频（发布台内直接选本地文件 → 自动入素材库并选中）
            pg.set_input_files("#pubFileInput", [str(b)])
            pg.wait_for_function("document.querySelector('#videoSel').value !== ''", timeout=10000)
            pg.wait_for_timeout(400)
            seltext = pg.eval_on_selector("#videoSel", "s=>s.selectedOptions[0].textContent")
            ok("本地选文件→自动选中", seltext == "测试视频B.mp4", seltext)
            ok("本地选文件→时长8秒", "8 秒" in pg.text_content("#videoMeta"), pg.text_content("#videoMeta"))
            pg.screenshot(path=str(SHOTS / "01b-发布台-本地选择.png"))
            # 切回素材库来源的视频A
            pg.select_option("#videoSel", str(vid))
            pg.wait_for_timeout(300)
            ok("视频已选（显示时长）", "12 秒" in pg.text_content("#videoMeta"), pg.text_content("#videoMeta"))

            # 通用文案
            pg.fill("#fTitle", "防水施工三大坑")
            pg.fill("#fBody", "第一：基层没处理干净；第二：材料用错；第三：节点没收口。")
            pg.fill("#fTopics", "装修 防水 避坑")
            pg.wait_for_timeout(200)
            pg.screenshot(path=str(SHOTS / "01-发布台-通用编辑.png"))

            # 一键生成
            pg.click("#genAllBtn")
            pg.wait_for_timeout(400)
            ok("生成后 8 个平台带 ✓", pg.eval_on_selector_all(".tab.has", "els=>els.length") == 8,
               pg.eval_on_selector_all(".tab.has", "els=>els.length"))

            pg.click('.tab[data-k="channels"]')
            pg.wait_for_timeout(250)
            ok("视频号·标题=通用", pg.input_value("#fTitle") == "防水施工三大坑")
            ok("视频号·话题格式 #a #b", pg.input_value("#fTopics") == "#装修 #防水 #避坑",
               pg.input_value("#fTopics"))

            pg.click('.tab[data-k="weibo"]')
            pg.wait_for_timeout(250)
            ok("微博·话题格式 #a# #b#", pg.input_value("#fTopics") == "#装修# #防水# #避坑#",
               pg.input_value("#fTopics"))

            # 抖音手动微调
            pg.click('.tab[data-k="douyin"]')
            pg.wait_for_timeout(250)
            ok("抖音·标题=通用（生成）", pg.input_value("#fTitle") == "防水施工三大坑")
            pg.fill("#fTitle", "抖音手改标题-01")
            pg.wait_for_timeout(200)
            pg.screenshot(path=str(SHOTS / "02-发布台-生成后.png"))

            # 切回通用：内容仍是原稿
            pg.click('.tab[data-k="common"]')
            pg.wait_for_timeout(250)
            ok("切换不串稿（通用标题不变）", pg.input_value("#fTitle") == "防水施工三大坑",
               pg.input_value("#fTitle"))

            # 保存草稿
            pg.click("#saveDraftBtn")
            pg.wait_for_timeout(500)
            ok("保存提示", "已保存" in pg.text_content("#draftState"), pg.text_content("#draftState"))

            # 刷新恢复（真刷新，触发从服务端恢复草稿）
            pg.reload()
            pg.wait_for_timeout(1000)
            ok("重载后·通用标题恢复", pg.input_value("#fTitle") == "防水施工三大坑", pg.input_value("#fTitle"))
            ok("重载后·视频选择恢复", pg.input_value("#videoSel") == str(vid), pg.input_value("#videoSel"))
            ok("重载后·显示上次保存", "上次保存" in pg.text_content("#draftState"), pg.text_content("#draftState"))
            pg.screenshot(path=str(SHOTS / "03-发布台-草稿恢复.png"))
            pg.click('.tab[data-k="douyin"]')
            pg.wait_for_timeout(250)
            ok("重载后·抖音微调仍在", pg.input_value("#fTitle") == "抖音手改标题-01", pg.input_value("#fTitle"))
            ok("无 JS 报错", not errs, errs)
            br.close()

        # API 侧复核
        with urllib.request.urlopen(base + "/api/draft", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
        ok("API 草稿数据已落库",
           (d.get("draft") or {}).get("data", {}).get("copy", {}).get("douyin", {}).get("title") == "抖音手改标题-01")
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

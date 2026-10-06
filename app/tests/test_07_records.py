# -*- coding: utf-8 -*-
"""开发项 S7 自测：发布记录（列表/筛选/统计/重试/导出 CSV）

运行：python tests/test_07_records.py
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
PORT = 8961
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


def api(base, path, data=None):
    if data is None:
        req = urllib.request.Request(base + path)
    else:
        req = urllib.request.Request(base + path, data=json.dumps(data).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_batch(base, batch, timeout=90):
    end = time.time() + timeout
    last = []
    while time.time() < end:
        last = api(base, "/api/publish/batch?id=%d" % batch)["jobs"]
        if all(r["status"] in ("success", "fail", "canceled", "manual") for r in last):
            return last
        time.sleep(1)
    return last


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "data").mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    (TMP / "data" / "config.json").write_text(json.dumps({"发布间隔": [1, 2], "单账号日更上限": 99}, ensure_ascii=False), encoding="utf-8")
    a = TMP / "in" / "测试视频A.mp4"
    a.parent.mkdir(parents=True, exist_ok=True)
    build_mp4(a, seconds=12, width=1080, height=1920)

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
        data = a.read_bytes()
        req = urllib.request.Request(base + "/api/videos/upload?name=" + urllib.parse.quote("测试视频A.mp4"),
                                     data=data, method="POST")
        vid = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())["video"]["id"]
        copies = {"common": {"title": "记录测试标题", "body": "", "topics": ""}}

        wait_batch(base, api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["douyin", "channels"], "mode": "now", "mock": True, "copies": copies})["batch"])
        b2 = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["douyin"], "mode": "now", "mock": True,
            "copies": {"common": {"title": "[fail] 失败演示", "body": "", "topics": ""}}})["batch"]
        wait_batch(base, b2)

        rec = api(base, "/api/records")
        ok("记录接口统计", rec["stats"]["total"] >= 3 and rec["stats"]["success"] >= 2 and rec["stats"]["fail"] >= 1,
           rec["stats"])
        fail_row = [r for r in rec["records"] if r["status"] == "fail"][0]
        ok("失败行有错误说明", bool(fail_row.get("error")), fail_row)

        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/#records")
            pg.wait_for_timeout(900)
            ok("记录页可见", pg.is_visible("#page-records"))
            stats = pg.text_content("#recStats")
            ok("统计条显示", "共" in stats and "成功率" in stats, stats)
            n_all = pg.eval_on_selector_all("#recBody tr", "els=>els.length")
            ok("列表行数 ≥3", n_all >= 3, n_all)
            pg.select_option("#recStatus", "fail")
            pg.wait_for_timeout(400)
            n_fail = pg.eval_on_selector_all("#recBody tr", "els=>els.length")
            ok("筛选失败=1 行", n_fail == 1, n_fail)
            pg.select_option("#recStatus", "")
            pg.fill("#recKw", "不存在的关键词xyz")
            pg.wait_for_timeout(400)
            ok("搜索无结果=0 行", pg.eval_on_selector_all("#recBody tr", "els=>els.length") == 0)
            pg.fill("#recKw", "")
            pg.wait_for_timeout(400)
            pg.screenshot(path=str(SHOTS / "26-记录页.png"))

            # 导出 CSV
            with pg.expect_download(timeout=8000) as dl_info:
                pg.click("#recExportBtn")
            dl = dl_info.value
            csv_path = TMP / "export.csv"
            dl.save_as(str(csv_path))
            raw = csv_path.read_bytes()
            ok("CSV 导出成功", raw.startswith("\ufeff".encode("utf-8")) and "时间,批次".encode("utf-8") in raw
               and "抖音".encode("utf-8") in raw, raw[:120])

            # 重试失败行
            pg.select_option("#recStatus", "fail")
            pg.wait_for_timeout(400)
            pg.click(".rec-retry")
            pg.wait_for_timeout(600)
            fixed = False
            for _ in range(40):
                time.sleep(1)
                rr = api(base, "/api/records")
                row = [r for r in rr["records"] if r["id"] == fail_row["id"]][0]
                if row["status"] == "success":
                    fixed = True
                    break
            ok("重试后转为成功", fixed, row["status"])
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

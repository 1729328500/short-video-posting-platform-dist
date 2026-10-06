# -*- coding: utf-8 -*-
"""开发项 S4 自测：封面（截取当前帧 / 自定义上传）+ 发布前预检

运行：python tests/test_03_cover_precheck.py
依赖：imageio-ffmpeg（生成真实测试视频）、Playwright + msedge。
"""
import base64
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8950
TMP = APP / "tests" / "_tmp"
SHOTS = APP / "tests" / "shots"

sys.path.insert(0, str(APP / "tests"))
from gen_mp4 import build_mp4  # noqa: E402

RESULTS = []

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def make_real_video(path, seconds=3, size="320x240"):
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run(
            [exe, "-y", "-f", "lavfi", "-i", "testsrc=duration=%d:size=%s:rate=10" % (seconds, size),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
            capture_output=True, timeout=120)
        return r.returncode == 0 and path.exists() and path.stat().st_size > 0
    except Exception as e:  # noqa: BLE001
        print("make_real_video failed:", e)
        return False


def upload(base, filepath):
    data = Path(filepath).read_bytes()
    url = base + "/api/videos/upload?name=" + urllib.parse.quote(Path(filepath).name)
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def api(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    vdir = TMP / "in"
    vdir.mkdir()

    real = vdir / "真实视频A.mp4"
    ok("生成真实测试视频(ffmpeg)", make_real_video(real, seconds=3))
    longv = vdir / "测试长视频.mp4"
    build_mp4(longv, seconds=9999, width=640, height=360)
    mkv = vdir / "素材C.mkv"
    mkv.write_bytes(b"\x00" * 256)
    png = vdir / "封面图.png"
    png.write_bytes(PNG_1PX)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(TMP / "data"), "--no-browser"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT,
    )
    base = "http://127.0.0.1:%d" % PORT
    up_ok = False
    for _ in range(40):
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=1) as r:
                if json.loads(r.read().decode("utf-8"))["ok"]:
                    up_ok = True
                    break
        except Exception:
            time.sleep(0.25)
    ok("服务启动", up_ok)
    if not up_ok:
        proc.terminate()
        return finish()

    try:
        up = upload(base, real)
        ok("上传真实视频(解析时长≈3s)", up.get("ok") and up["video"].get("duration") is not None
           and abs(up["video"]["duration"] - 3.0) < 0.6, up.get("video"))
        vid1 = up["video"]["id"]

        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/")
            pg.wait_for_timeout(800)

            card = pg.locator(".vcard").first
            ok("无封面时占位▶", "▶" in card.locator(".vthumb").inner_text())

            # ---- 封面：截取当前帧 ----
            card.locator(".act-cover").click()
            pg.wait_for_timeout(400)
            ok("封面弹层打开", pg.is_visible("#cvVideo"))
            pg.wait_for_function("(v=>v&&v.readyState>=2)(document.getElementById('cvVideo'))", timeout=12000)
            pg.eval_on_selector("#cvVideo", "v=>{v.currentTime=1;}")
            pg.wait_for_timeout(500)
            pg.screenshot(path=str(SHOTS / "01-封面设置.png"))
            pg.click("#cvCaptureBtn")
            pg.wait_for_function("!document.getElementById('mask').classList.contains('open')", timeout=12000)
            pg.wait_for_timeout(500)
            v0 = api(base, "/api/videos")["videos"][0]
            ok("截帧后封面记录(.jpg)", (v0.get("cover") or "").endswith(".jpg"), v0.get("cover"))
            cov = urllib.request.urlopen(base + "/api/videos/cover/" + str(vid1), timeout=5)
            ok("封面可访问且为 jpeg", cov.status == 200 and cov.headers.get("Content-Type") == "image/jpeg",
               cov.headers.get("Content-Type"))
            clen = len(cov.read())
            ok("封面内容非空", clen > 200, clen)
            pg.wait_for_function("(i=>i&&i.complete&&i.naturalWidth>0)(document.querySelector('.vcard img.vthumbimg'))",
                                 timeout=8000)
            pg.screenshot(path=str(SHOTS / "02-封面缩略图.png"))

            # ---- 封面：上传自定义图片 ----
            pg.locator(".vcard").first.locator(".act-cover").click()
            pg.wait_for_timeout(400)
            pg.set_input_files("#imageInput", [str(png)])
            pg.wait_for_function("!document.getElementById('mask').classList.contains('open')", timeout=8000)
            pg.wait_for_timeout(400)
            v0 = api(base, "/api/videos")["videos"][0]
            ok("自定义封面记录(.png)", (v0.get("cover") or "").endswith(".png"), v0.get("cover"))

            # ---- 预检：正常视频应全过 ----
            pg.locator(".vcard").first.locator(".act-precheck").click()
            pg.wait_for_timeout(600)
            cnt = pg.eval_on_selector_all(".pkrow", "els=>els.length")
            ok("预检 8 平台行", cnt == 8, cnt)
            body = pg.eval_on_selector("#mBody", "e=>e.innerText")
            ok("正常视频全过", "全部通过" in body, body[:140])
            pg.click("#mFoot button")
            pg.wait_for_timeout(300)
            ok("预检弹层关闭", not pg.evaluate("document.getElementById('mask').classList.contains('open')"))

            # ---- 预检：超长视频告警 ----
            ok("上传超长视频", upload(base, longv).get("ok"))
            pg.reload()
            pg.wait_for_timeout(900)
            row = pg.locator(".vcard").filter(has_text="测试长视频")
            row.locator(".act-precheck").click()
            pg.wait_for_timeout(600)
            warns = pg.eval_on_selector_all(".badge.warn", "els=>els.length")
            ok("超长视频 ≥5 平台告警", warns >= 5, warns)
            dy = pg.locator(".pkrow").filter(has_text="抖音").inner_text()
            ok("抖音行提示超时长", "超过上限" in dy, dy)
            pg.screenshot(path=str(SHOTS / "03-预检警示.png"))
            pg.click("#mFoot button")
            pg.wait_for_timeout(300)

            # ---- 预检：不支持格式 ----
            ok("上传 mkv（占位文件）", upload(base, mkv).get("ok"))
            pg.reload()
            pg.wait_for_timeout(900)
            row = pg.locator(".vcard").filter(has_text="素材C")
            row.locator(".act-precheck").click()
            pg.wait_for_timeout(600)
            dy = pg.locator(".pkrow").filter(has_text="抖音").inner_text()
            ok("mkv 抖音行提示格式", "格式" in dy, dy)
            pg.click("#mFoot button")
            pg.wait_for_timeout(300)

            # ---- 删除视频应同步清掉封面（轮询等待完成，避免时序偶发） ----
            pg.locator(".vcard").filter(has_text="真实视频A").locator(".act-del").click()
            pg.wait_for_timeout(500)
            gone = False
            for _ in range(12):
                try:
                    urllib.request.urlopen(base + "/api/videos/cover/" + str(vid1), timeout=5)
                    gone = False
                except urllib.error.HTTPError as e:
                    gone = (e.code == 404)
                if gone:
                    break
                time.sleep(0.5)
            ok("删除后封面 404", gone)
            covers_dir = TMP / "data" / "covers"
            covers = []
            for _ in range(12):
                covers = [c for c in covers_dir.glob("*") if not c.name.startswith(".")] if covers_dir.exists() else []
                if len(covers) == 0:
                    break
                time.sleep(0.5)
            ok("封面文件已清理", len(covers) == 0, [c.name for c in covers])

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

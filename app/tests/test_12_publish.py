# -*- coding: utf-8 -*-
"""v1.2 自测：发布执行器（适配器驱动 · 填好停在提交前 · 绝不自动提交）

运行：python tests/test_12_publish.py
依赖：Playwright + msedge

背景（用户 2026-09-27 拍板）：真实发布先做「填好停在提交前」——
工具负责打开发布页、投递视频、填标题/正文/话题、把提交按钮滚到眼前，
**提交由人工点**。本测试用一个选择器与抖音真机一致的模拟发布页来回归这条链路，
并用「/mock/published 被访问过没有」直接证明工具没有越界提交。
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8977
TMP = APP / "tests" / "_tmp"
SHOTS = APP / "tests" / "shots"
VID = APP / "tests" / "_tmp" / "in" / "发布回归视频.mp4"

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def must_exist(path, note):
    if not path.exists():
        print("  缺少依赖：%s（%s）" % (path, note))
    return path.exists()


def build_video(p):
    """造一个真能播的 mp4（发布页要真视频）"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        r = subprocess.run([exe, "-y", "-f", "lavfi", "-i", "testsrc=duration=3:size=480x640:rate=20",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
                           capture_output=True, timeout=180)
        return r.returncode == 0 and p.exists() and p.stat().st_size > 0
    except Exception as e:  # noqa: BLE001
        print("  造测试视频失败:", e)
        return False


def main():
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    (TMP / "data").mkdir(parents=True)
    (TMP / "in").mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    ok("能造出真视频（imageio-ffmpeg）", build_video(VID))

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

    status = TMP / "job.json"
    try:
        r = subprocess.run(
            [sys.executable, str(APP / "publish_worker.py"),
             "--key", "douyin",
             "--engine", "legacy",   # 走本项目手写引擎打模拟页；不指定会走 SAU 去真发抖音                       # 用抖音适配器（选择器与模拟页一致）
             "--url", base + "/mock/publish",
             "--profile", str(TMP / "data" / "browsers" / "douyin"),
             "--video", str(VID),
             "--title", "回归标题ABC",
             "--body", "回归正文第一句。",
             "--topics", "#回归 #自动化",
             "--status", str(status),
             "--hold", "6", "--headless"],
            cwd=str(APP), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300)
        st = {}
        try:
            st = json.loads(status.read_text(encoding="utf-8"))
        except Exception as e:
            print("  读状态失败:", e, (r.stdout or "")[-300:], (r.stderr or "")[-300:])

        steps = st.get("steps") or []
        joined = " | ".join(steps)
        ok("走到「待人工确认」态", st.get("state") == "manual", st)
        ok("识别为已登录（复用登录判据）", "登录态正常" in joined, joined)
        ok("投递了视频", "视频已投递" in joined, joined)
        ok("填了标题", "标题已填" in joined, joined)
        ok("填了正文", "正文已填" in joined, joined)
        ok("关了话题联想下拉", "关闭话题联想下拉" in joined, joined)
        ok("把提交按钮滚到视野内", "滚到视野内" in joined, joined)
        # 文案要说清「工具不提交」——窗口关闭后的收尾文案也得保留这句，
        # 否则用户看到「窗口已关闭」会以为工具发过了
        ok("提示里说明提交由人工点", "不点提交" in (st.get("note") or ""), st.get("note"))

        # ★ 最关键：工具绝不能自己点提交
        ok("工具没有越界提交（/mock/published 未被访问）",
           not (TMP / "data" / "_mock_submitted.txt").exists())
        ok("留了截图存档", Path(str(status).replace(".json", ".png")).exists())

        # 适配器配置本身
        ad = json.loads((APP / "publish_adapters.json").read_text(encoding="utf-8"))
        ok("适配器配置含 8 个平台", len(ad.get("platforms") or {}) == 8, list(ad.get("platforms") or {}))
        ok("抖音已标记为已校准", (ad["platforms"]["douyin"].get("calibrated") is True))
        uncal = [k for k, v in ad["platforms"].items() if not v.get("calibrated")]
        print("  （待校准平台 %d 个：%s）" % (len(uncal), "、".join(uncal)))

        # ── 二次验证（风控）场景：平台弹短信验证码时，工具必须【停住并说清楚】 ──
        #    真机实测（2026-09-27）：抖音首次发布能过，紧接着第二次就弹短信验证码，
        #    这个码只有账号本人能拿到 —— 工具绝不能装作发成功了。
        status2 = TMP / "job_captcha.json"
        subprocess.run(
            [sys.executable, str(APP / "publish_worker.py"),
             "--key", "douyin",
             "--engine", "legacy",   # 走本项目手写引擎打模拟页；不指定会走 SAU 去真发抖音
             "--url", base + "/mock/publish?captcha=1",
             "--profile", str(TMP / "data" / "browsers" / "douyin"),
             "--video", str(VID),
             "--title", "验证码场景",
             "--body", "x", "--topics", "",
             "--status", str(status2),
             "--submit", "--hold", "5", "--headless"],
            cwd=str(APP), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300)
        st2 = {}
        try:
            st2 = json.loads(status2.read_text(encoding="utf-8"))
        except Exception as e:
            print("  读状态失败:", e)
        steps2 = " | ".join(st2.get("steps") or [])
        ok("遇到二次验证时识别出来", "检测到平台二次验证弹窗" in steps2, steps2)
        ok("二次验证时状态为待人工（不谎报成功）", st2.get("state") == "manual", st2.get("state"))
        ok("提示里说明验证码要本人填", "短信二次验证" in (st2.get("note") or ""), st2.get("note"))
        ok("二次验证时没有越界提交", not (TMP / "data" / "_mock_submitted.txt").exists())
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

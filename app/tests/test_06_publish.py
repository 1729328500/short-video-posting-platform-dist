# -*- coding: utf-8 -*-
"""开发项 S6 自测：发布执行（队列 / 模拟执行 / 定时 / 取消 / 重试 / 完成通知 dry-run / 未登录拦截）

运行：python tests/test_06_publish.py
"""
import base64
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8958
TMP = APP / "tests" / "_tmp"
SHOTS = APP / "tests" / "shots"

sys.path.insert(0, str(APP))
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
        j = api(base, "/api/publish/batch?id=%d" % batch)
        last = j["jobs"]
        # ★ 空列表别当成"跑完了"（OCR 2026-09-30 指出）：all() 对空序列恒真，
        #   一次瞬时空响应就会让 wait_batch 立刻返回 []，后面所有断言**假通过**。
        if last and all(r["status"] in ("success", "fail", "canceled", "manual") for r in last):
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
        copies = {"common": {"title": "发布测试标题", "body": "正文", "topics": "装修 防水"}}

        # A. 多平台模拟发布
        j = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["douyin", "channels", "bilibili"],
            "mode": "now", "mock": True, "copies": copies})
        ok("创建批次（3 平台）", j.get("ok") and j.get("count") == 3, j)
        b1 = j["batch"]
        jobs = wait_batch(base, b1)
        ok("批次全部成功", bool(jobs) and all(r["status"] == "success" for r in jobs),
           [(r["platform"], r["status"]) for r in jobs])
        # ★ 2026-09-30 实测事故：模拟执行原来把 step 写成「已发布 ✓」，和真发一模一样 ——
        #   用户整批发布全走了模拟却以为发出去了（平台上什么都没有）。
        #   判据从事实派生：只要这条是 mock，文案就必须带「模拟」二字。
        #   ★ `bool(jobs) and`：空列表不许假通过（OCR 同批指出）。
        ok("模拟任务如实标注（不再谎报「已发布」）",
           bool(jobs) and all("模拟" in (r["step"] or "") for r in jobs), [r["step"] for r in jobs])
        ok("记录接口带回 mock 标记（界面据此打「模拟」徽章）",
           bool(jobs) and all(r.get("mock") == 1 for r in jobs), [r.get("mock") for r in jobs])
        # ★ 事故根因：发布页「模拟执行」复选框**默认勾选**，页面一刷新就回到勾选态
        #   ⇒ 用户无感地整批走模拟。默认必须是不勾（真发是常态，模拟是例外）。
        #   ★ 解析标签本身，别钉死属性顺序（OCR 同批指出）：`<input checked id="pubMock">`
        #     也是默认勾选，子串匹配会漏 ⇒ 守门判据必须看标签里有没有 checked。
        _idx = (APP / "ui" / "index.html").read_text(encoding="utf-8")
        _tag = re.search(r'<input[^>]*id="pubMock"[^>]*>', _idx)
        ok("「模拟执行」默认不勾选（刷新不再偷偷回到模拟）",
           bool(_tag) and "checked" not in _tag.group(0),
           _tag.group(0) if _tag else "index.html 里没找到 pubMock")

        nlog = TMP / "data" / "notify.log"
        got = False
        for _ in range(15):
            time.sleep(1)
            if nlog.exists() and ("batch=%d" % b1) in nlog.read_text(encoding="utf-8"):
                got = True
                break
        ok("完成通知已记录(dry-run)", got)
        txt = nlog.read_text(encoding="utf-8") if nlog.exists() else ""
        ok("dry-run 未真实发送", "dry-run" in txt, txt[-120:])

        # B. 失败 + 重试
        j = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["douyin"], "mode": "now", "mock": True,
            "copies": {"common": {"title": "[fail] 失败演示", "body": "", "topics": ""}}})
        b2 = j["batch"]
        jobs2 = wait_batch(base, b2)
        # ★ 空列表一律不许假通过、也不许 IndexError 把整轮跑挂掉（OCR 2026-09-30 指出：
        #   同一个文件里我只补了一处，这里几处是同一个毛病）。
        ok("模拟失败出现", bool(jobs2) and jobs2[0]["status"] == "fail", jobs2[:1])
        r = api(base, "/api/publish/retry", {"id": jobs2[0]["id"] if jobs2 else 0})
        ok("重试已排队", r.get("ok"), r)
        jobs2b = wait_batch(base, b2)
        ok("重试后成功",
           bool(jobs2b) and jobs2b[0]["status"] == "success" and jobs2b[0]["retries"] >= 1,
           [(x["status"], x["retries"]) for x in jobs2b[:1]])

        # C. 定时发布（4 秒后）
        at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 4))
        j = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["kuaishou"], "mode": "schedule",
            "scheduled_at": at, "mock": True, "copies": copies})
        b3 = j["batch"]
        jobs3 = wait_batch(base, b3)
        ok("定时任务到点执行", bool(jobs3) and jobs3[0]["status"] == "success", jobs3[:1])

        # D. 取消
        at2 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 3600))
        j = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["weibo", "xigua"], "mode": "schedule",
            "scheduled_at": at2, "mock": True, "copies": copies})
        b4 = j["batch"]
        api(base, "/api/publish/cancel", {"batch": b4})
        jobs4 = api(base, "/api/publish/batch?id=%d" % b4)["jobs"]
        ok("取消未执行项", bool(jobs4) and all(r["status"] == "canceled" for r in jobs4),
           [r["status"] for r in jobs4])

        # E. 真实模式未登录拦截
        blocked = False
        err = ""
        try:
            api(base, "/api/publish/create", {
                "video_id": vid, "platforms": ["douyin"], "mode": "now", "mock": False, "copies": copies})
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode("utf-8"))
            blocked = e.code == 400
            err = body.get("error", "")
        ok("真实模式未登录被拦截", blocked and "登录" in err, err)

        # E2. 封面硬闸（2026-09-30）：**微博没封面发不出去**（SAU 的微博上传器
        #     validate_upload_args 直接 raise），所以在 create 这层就拦 ——
        #     而且要**只拦微博**（其它家不给封面都能发，别误伤）。
        def _create(plats, mk):
            try:
                api(base, "/api/publish/create", {
                    "video_id": vid, "platforms": plats, "mode": "now",
                    "mock": mk, "copies": copies})
                return 200, ""
            except urllib.error.HTTPError as e:
                return e.code, (json.loads(e.read().decode("utf-8")).get("error") or "")

        _c1, _e1 = _create(["weibo"], False)
        ok("微博 + 没封面 → create 就被拦下并说清怎么办", _c1 == 400 and "封面" in _e1, (_c1, _e1))
        _c2, _e2 = _create(["weibo"], True)
        ok("模拟发布不掺和封面闸", _c2 == 200, (_c2, _e2))
        _c3, _e3 = _create(["douyin"], False)
        ok("别的平台不误伤（抖音没封面只报登录，不报封面）",
           _c3 == 400 and "登录" in _e3 and "封面" not in _e3, (_c3, _e3))
        # 真给它设一张封面 → 封面闸必须放行（拦截原因回到登录态）
        _jpg = base64.b64decode(
            "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
            "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
            "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q==")
        _req = urllib.request.Request(base + "/api/videos/cover?id=%d" % vid, data=_jpg,
                                      method="POST", headers={"Content-Type": "image/jpeg"})
        with urllib.request.urlopen(_req, timeout=20) as _r:
            ok("给视频设封面（供下面验放行）", json.loads(_r.read().decode("utf-8")).get("ok"))
        _c4, _e4 = _create(["weibo"], False)
        ok("设了封面之后：微博不再被封面闸拦（原因回到登录态）",
           _c4 == 400 and "封面" not in _e4, (_c4, _e4))

        # E3. 界面侧的 (b)+(c)：自动抽帧兜底 + 抽不出来时的拦截文案（源码级钉住）
        _ui2 = (APP / "ui" / "app.js").read_text(encoding="utf-8")
        ok("界面：必须封面的平台**只有微博**（查证：其它家不给都能发）",
           "var COVER_REQUIRED = ['weibo']" in _ui2)
        ok("界面：勾了微博而没封面时先自动抽第 1 帧兜底（b）",
           "async function autoCover(" in _ui2 and "await autoCover(video)" in _ui2)
        ok("界面：抽帧失败会拦下并给明确指引（c）", "微博必须有封面：请先去素材页" in _ui2)

        # E4. 发完之后**不许再空等**（2026-09-30 用户报：头条/B站发成功、发布台一直等待）。
        #     根因：--hold 默认 900 秒，自动提交模式下活干完了还摁着窗口 —— 引擎只能干等，
        #     用户手动中止才收尾（job #74/#75 日志里的「被停时 worker 已给出终态」）。
        class _Ctx:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        class _Pg:
            url = "https://example.com/manage"

        class _A:
            hold = 900
            status = str(APP / "tests" / "_tmp27_hold.json")
            # ★ 2026-10-06：_hold 收尾要调 browser_state.close(ctx, args.profile)，
            #   少了这个属性会抛 AttributeError、被那层的 except 吞掉 →
            #   「把浏览器关了」这条断言**永远不可能通过**（在 f33c0b2 基线上实测同样失败）。
            profile = ""

        import publish_worker as _pw
        _ctx2, _t0 = _Ctx(), time.time()
        _pw._hold(_ctx2, _Pg(), _A(), [], "测试平台", final_state="success",
                  final_note="测试", need_hold=False)
        _dt = time.time() - _t0
        ok("自动提交后 _hold 立刻返回（不再空等 15 分钟）", _dt < 3, "耗时 %.1fs" % _dt)
        ok("并且把浏览器关了（不留孤儿）", _ctx2.closed)
        # ★ 安全网别一起拆了（OCR 2026-09-30 指出：原来数"裸调用"个数 == 2 是错的 ——
        #   二次验证那处带 kwargs 数不到，而且给已有调用加个参数就会假失败）。
        #   改成两条稳的：① 四个 hold 调用点**一个都不能少**；② 全文件只有一处
        #   按 --submit 决定 hold（就是"自动提交完成"那处）。
        _src_pw = (APP / "publish_worker.py").read_text(encoding="utf-8")
        # 排除**函数定义**那一行（`def _hold(ctx, page, args, steps, name…` 也含这个前缀）
        _call_sites = [ln for ln in _src_pw.split(chr(10))
                       if "_hold(ctx, page, args, steps, name" in ln
                       and not ln.strip().startswith("def ")]
        ok("4 个 hold 调用点都在（3 个默认 hold + 1 个按 submit 决定）",
           len(_call_sites) == 4, len(_call_sites))
        ok("只有「自动提交完成」那处传 need_hold=not args.submit",
           _src_pw.count("need_hold=not args.submit") == 1,
           _src_pw.count("need_hold=not args.submit"))

        # F. UI 全流程
        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.on("console", lambda m: print("[PAGE]", (m.text or "")[:140]))
            pg.goto(base + "/#publish")
            pg.wait_for_timeout(1000)
            pg.select_option("#videoSel", str(vid))
            pg.wait_for_timeout(400)
            pg.check('.pubplat[data-k="douyin"]')
            pg.check('.pubplat[data-k="channels"]')
            # ★ 2026-09-30：模拟执行**不再默认勾选**（默认=真发）。这个 UI 用例在测试环境里
            #   没有任何平台登录，只能走模拟路径 —— 必须**显式**勾上。原来靠默认值蒙混，
            #   等于这一条从来没测过"模拟"这个开关；顺带把确认弹层的提示也验了。
            pg.check("#pubMock")
            pg.click("#pubStartBtn")
            pg.wait_for_timeout(600)
            ok("确认弹层出现", pg.is_visible("#mask.open"))
            body = pg.eval_on_selector("#mBody", "e=>e.innerText")
            ok("确认弹层含平台行", "抖音" in body and "视频号" in body, body[:120])
            ok("确认弹层明说「模拟执行·不真实提交」",
               "模拟执行" in body and "不真实提交" in body, body[:120])
            pg.screenshot(path=str(SHOTS / "24-发布确认.png"))
            pg.locator("#mFoot button").filter(has_text="确认发布").click()
            pg.wait_for_timeout(1200)
            ok("进度卡出现", pg.is_visible("#pubProgressCard"))
            done = False
            for _ in range(60):
                time.sleep(1)
                t = pg.eval_on_selector("#pubProgress", "e=>e.innerText")
                print("  t: %s" % t.replace(chr(10), " / ")[:110])
                if "成功 ✓" in t and "排队中" not in t:
                    done = True
                    break
            ok("UI 批次跑完", done, pg.eval_on_selector("#pubProgress", "e=>e.innerText"))
            acts = ""
            for _ in range(40):
                time.sleep(1)
                acts = pg.eval_on_selector("#pubActions", "e=>e.innerText")
                if "本批次完成" in acts:
                    break
            pg.screenshot(path=str(SHOTS / "25-发布进度.png"))
            ok("完成汇总可见", "本批次完成" in acts, acts)
            ok("无 JS 报错", not errs, errs)
            br.close()

        # G. 单账号日更上限（v1.0.1：设置项实装校验）
        api(base, "/api/config/save", {"data": {"单账号日更上限": 2}})
        blocked2 = False
        err2 = ""
        try:
            api(base, "/api/publish/create", {
                "video_id": vid, "platforms": ["douyin"], "mode": "now", "mock": True, "copies": copies})
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode("utf-8"))
            blocked2 = e.code == 400
            err2 = body.get("error", "")
        ok("超日更上限被拦截", blocked2 and "上限" in err2, err2)

        # H. ★ 失败的任务**不该**占用日更名额（v1.5 修复）
        #    旧写法 `status!='canceled'` 把 fail 也算作「今天已发」——
        #    实测踩过：两条失败任务让日更上限 2 直接满，用户一整天发不了。
        api(base, "/api/config/save", {"data": {"单账号日更上限": 2}})
        jf = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["xhs"], "mode": "now", "mock": True,
            "copies": {"common": {"title": "[fail] 占额测试", "body": "", "topics": ""}}})
        jobs_f = wait_batch(base, jf["batch"])
        ok("造出一条失败任务", bool(jobs_f) and jobs_f[0]["status"] == "fail", jobs_f[:1])

        made, err3 = 0, ""
        for i in range(2):
            try:
                api(base, "/api/publish/create", {
                    "video_id": vid, "platforms": ["xhs"], "mode": "now", "mock": True,
                    "copies": {"common": {"title": "占额后正常发布 %d" % i, "body": "", "topics": ""}}})
                made += 1
            except urllib.error.HTTPError as e:
                err3 = json.loads(e.read().decode("utf-8")).get("error", "")
        ok("失败的任务不占日更名额（失败后仍能发满 2 条）", made == 2,
           "建成 %d 条；err=%s" % (made, err3))

        # I. ★ 配额按【账号】算，不是按平台（v1.6 修复）
        #    旧实现按平台计数 —— 换个账号进来配额不重置，用户被锁住还找不到原因。
        api(base, "/api/config/save", {"data": {"单账号日更上限": 2}})
        nb = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["bilibili"], "mode": "now", "mock": True, "copies": copies})["batch"]
        wait_batch(base, nb)
        with sqlite3.connect(TMP / "data" / "app.db") as c:
            c.execute("UPDATE publishes SET account='ACC_A', account_name='账号A' WHERE platform='bilibili'")

        def set_account(name, aid):
            (TMP / "data" / "browsers").mkdir(parents=True, exist_ok=True)
            (TMP / "data" / "browsers" / "bilibili.status.json").write_text(
                json.dumps({"key": "bilibili", "state": "on", "account": aid, "account_name": name},
                           ensure_ascii=False), encoding="utf-8")

        def try_create():
            try:
                api(base, "/api/publish/create", {
                    "video_id": vid, "platforms": ["bilibili"], "mode": "now", "mock": True, "copies": copies})
                return True, ""
            except urllib.error.HTTPError as e:
                return False, json.loads(e.read().decode("utf-8")).get("error", "")

        set_account("账号B", "ACC_B")
        okB, errB = try_create()
        ok("换账号后配额重置（按账号算，不按平台）", okB, errB)

        set_account("账号A", "ACC_A")
        okA, errA = try_create()
        ok("切回已发满的账号 → 被拦，且提示里点名账号", (not okA) and ("账号A" in errA), errA)
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

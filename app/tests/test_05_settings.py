# -*- coding: utf-8 -*-
"""开发项 S9-a 自测：设置页（配置持久化 / 存储数据 / 日志 / 访问口令锁）

运行：python tests/test_05_settings.py
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8956
TMP = APP / "tests" / "_tmp"
SHOTS = APP / "tests" / "shots"

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


SESSION = {"token": ""}      # v1.7：设了口令后接口要求登录，测试要带上会话


def api(base, path, data=None, allow_fail=False):
    hdr = {"Content-Type": "application/json"} if data is not None else {}
    if SESSION["token"]:
        hdr["Cookie"] = "vp_session=" + SESSION["token"]
    req = urllib.request.Request(base + path,
                                 data=json.dumps(data).encode("utf-8") if data is not None else None,
                                 method="POST" if data is not None else "GET", headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if allow_fail:
            try:
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "code": e.code}
        raise


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "data").mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(TMP / "data"), "--no-browser"],
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
        # ---- API：默认配置 ----
        cfg = api(base, "/api/config")["config"]
        ok("默认配置（口令未设）", cfg.get("口令已设") is False, cfg)
        ok("默认播报时间 20:00", cfg.get("播报时间") == "20:00")

        # ---- API：保存 + 持久化 ----
        api(base, "/api/config/save", {"data": {
            "播报时间": "21:30", "发布间隔": [90, 240], "单账号日更上限": 3, "通知_飞书": False}})
        cfg = api(base, "/api/config")["config"]
        ok("保存后读取一致", cfg["播报时间"] == "21:30" and cfg["发布间隔"] == [90, 240]
           and cfg["单账号日更上限"] == 3 and cfg["通知_飞书"] is False, cfg)
        ok("配置文件已落盘", (TMP / "data" / "config.json").exists())

        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 860}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/#settings")
            pg.wait_for_timeout(900)

            ok("设置页可见", pg.is_visible("#page-settings"))
            cards = pg.eval_on_selector_all("#page-settings .card", "els=>els.length")
            ok("设置分区 ≥ 7 个", cards >= 7, cards)
            ok("无口令时不锁屏", not pg.is_visible("#lockMask"))
            ok("播报时间回显", pg.input_value("#setReport") == "21:30", pg.input_value("#setReport"))
            ok("间隔回显", pg.input_value("#setIntMin") == "90" and pg.input_value("#setIntMax") == "240")
            ok("飞书开关回显（关）", pg.is_checked("#setNotifyFs") is False)

            # ---- UI 修改 → 落盘 ----
            pg.fill("#setReport", "22:15")
            pg.dispatch_event("#setReport", "change")
            pg.wait_for_timeout(600)
            cfg = api(base, "/api/config")["config"]
            ok("UI 改时间后落盘", cfg["播报时间"] == "22:15", cfg["播报时间"])

            # ---- 存储信息 / 日志 ----
            d = api(base, "/api/data-info")
            ok("data-info 正常", d.get("ok") and str(d.get("dir", "")).replace("/", "\\").endswith("data")
               and d.get("videos") == 0, d)
            lg = api(base, "/api/logs/tail?lines=50")
            ok("日志接口正常", lg.get("ok") is True and isinstance(lg.get("text"), str))
            pg.click("#logRefreshBtn")
            pg.wait_for_timeout(400)
            # ★ 2026-10-06（OCR 收尾审查抓到）：原来是
            #     `assert ... > 0` 紧跟 `ok("日志框有点东西", True)` —— 恒真。
            #   两个后果：python -O 下 assert 被剥掉，这条检查彻底空转；
            #   而且 eval_on_selector 找不到元素时是**抛异常**（实测过），
            #   会直接崩掉 main()、连 FAIL 行和汇总都不打。
            #   折进 ok() 并把异常收成 0，它才真的在测东西、且失败时老实报 FAIL。
            try:
                _log_len = pg.eval_on_selector("#logBox", "e=>e.textContent.length")
            except Exception:
                _log_len = 0
            ok("日志框有点东西", _log_len > 0, _log_len)

            pg.screenshot(path=str(SHOTS / "22-设置页.png"))

            # ---- 访问口令 ----
            pg.fill("#setPass", "8888")
            pg.click("#savePassBtn")
            pg.wait_for_timeout(900)
            # 设口令的人当场获得会话（服务端会下发 Cookie）
            for c in pg.context.cookies():
                if c["name"] == "vp_session":
                    SESSION["token"] = c["value"]
            cfg = api(base, "/api/config")["config"]
            # ★ 2026-10-06（安全审查 R2）：口令哈希搬到 auth.json，不再在 config.json 里。
            #   这条判据的**本意**是"落盘的是加盐哈希、不是明文"，所以断言要跟着搬家，
            #   并且把本意写明确：哈希是 pbkdf2-sha256 的 64 位十六进制、有盐、没有明文字段。
            raw_cfg = (TMP / "data" / "config.json").read_text(encoding="utf-8")
            raw_auth = (TMP / "data" / "auth.json").read_text(encoding="utf-8")
            ra = json.loads(raw_auth)
            ok("口令已保存（加盐哈希·无明文）",
               cfg.get("口令已设") is True
               and len(str(ra.get("口令哈希") or "")) == 64
               and bool(ra.get("口令盐"))
               and "口令" not in ra                       # 不落明文字段
               and "8888" not in raw_auth and "8888" not in raw_cfg,
               (raw_cfg[-60:], raw_auth[-130:]))
            # v1.7：鉴权搬到服务端（会话 Cookie）。
            # ① 设口令的人当场即为已登录（有意行为：否则刚设完就被自己锁在外面）
            st = api(base, "/api/auth/status")
            ok("设完口令当场即为已登录", st["口令已设"] is True and st["logged_in"] is True, st)
            # ② 清掉会话后，业务接口必须被拒
            saved_tok = SESSION["token"]
            SESSION["token"] = ""
            bad = api(base, "/api/config", allow_fail=True)
            ok("未登录访问业务接口被拒（401 语义）", bad.get("need_login") is True, bad)
            login = api(base, "/api/auth/login", {"pass": "0000"}, allow_fail=True)
            ok("错口令不通过", login.get("ok") is False, login)
            SESSION["token"] = saved_tok
            ok("口令状态回显已启用", "已启用" in pg.text_content("#passState"),
               pg.text_content("#passState"))

            # ③ 会话是 Cookie 承载的（7 天）—— 刷新页面**不该**再要求输口令。
            #    这是有意的：登录一次就不该反复输。
            pg.reload()
            pg.wait_for_timeout(1000)
            ok("已登录时刷新页面不再要求输口令", not pg.is_visible("#lockMask"))

            # ④ 清掉 Cookie 模拟「换个浏览器/退出登录」→ 锁屏必须出现
            pg.context.clear_cookies()
            pg.reload()
            pg.wait_for_timeout(1000)
            ok("无会话时打开 → 锁屏出现", pg.is_visible("#lockMask"))
            pg.screenshot(path=str(SHOTS / "23-设置-口令锁.png"))
            pg.fill("#lockInput", "0000")
            pg.click("#lockBtn")
            pg.wait_for_timeout(800)
            ok("错口令仍锁", pg.is_visible("#lockMask"))
            pg.fill("#lockInput", "8888")
            pg.click("#lockBtn")
            pg.wait_for_timeout(2000)          # 成功后前端会 reload
            for c in pg.context.cookies():
                if c["name"] == "vp_session":
                    SESSION["token"] = c["value"]
            ok("对口令解锁（服务端会话生效）",
               (not pg.is_visible("#lockMask")) and bool(SESSION["token"]),
               pg.is_visible("#lockMask"))

            ok("无 JS 报错", not errs, errs)
            br.close()

        # 清掉口令（避免影响其他测试）
        api(base, "/api/config/save", {"data": {"口令": ""}})
        cfg = api(base, "/api/config")["config"]
        ok("口令可清除", cfg.get("口令已设") is False, cfg)

        # ★ 2026-09-30（OCR 抓到的真问题）：norm_config() 是**写死的透传表** —— 新键不加进去，
        #   用户在设置页改的值保存时会被静默丢回默认（改了等于没改）。这条判据钉的就是它：
        #   走一遍真实的 保存 → 读回，值必须还在。
        api(base, "/api/config/save", {"data": {"数据统计范围": 7}})
        _c2 = (api(base, "/api/config") or {}).get("config") or {}
        ok("设置「数据统计范围」存得住（不被 norm_config 丢掉）",
           _c2.get("数据统计范围") == 7, _c2.get("数据统计范围"))
        api(base, "/api/config/save", {"data": {"数据统计范围": 99}})
        _c3 = (api(base, "/api/config") or {}).get("config") or {}
        ok("越界被夹到 1~30（99 → 30）", _c3.get("数据统计范围") == 30, _c3.get("数据统计范围"))
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

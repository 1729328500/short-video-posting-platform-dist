# -*- coding: utf-8 -*-
"""自测：AI 配置（地址/模型/密钥）能在设置页配 —— 且密钥绝不外泄

运行：python tests/test_17_ai_config.py

★ 为什么需要这个测试（2026-09-28）：
  「⚡ 一键生成各平台适配版」靠 AI 改写，但整个应用**没有配置密钥的入口** ——
  设置页的 AI 卡片只有「通道状态 + 启用开关」，`/api/ai/save` 也只收 `enabled`
  （`save_ai_enabled` 只写这一个字段）。于是 key 只能 SSH 上 NAS 手改 data/ai.json，
  新机器上这个功能永远是「未配置」，只能退化成基础版。

判据：
  ① 地址/模型/密钥能存下来，且 status 能报出「已配置」；
  ② ★ 密钥只进不出：任何接口的响应里都不许出现密钥明文；
  ③ 留空＝**不改动**已有密钥（不是清空）—— 否则改个模型就会把密钥抹掉；
  ④ 设置页真的有这几个输入框，能改动生效；
  ⑤ 旧前端行为兼容（只发 {enabled} 也不报错）。
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8983
TMP = APP / "tests" / "_tmp17"
DATA = TMP / "data"

KEY = "sk-testonly0000000000000000000000"
RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def raw(base, path, data=None):
    """返回原始响应文本（查密钥有没有外泄要用原文）"""
    if data is None:
        req = urllib.request.Request(base + path)
    else:
        req = urllib.request.Request(base + path, data=json.dumps(data).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def api(base, path, data=None):
    return json.loads(raw(base, path, data))


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    DATA.mkdir(parents=True)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(DATA), "--no-browser", "--notify-dry-run"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % PORT
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            up = True
            break
        except Exception:
            time.sleep(0.5)
    ok("测试服务起来了", up)
    if not up:
        proc.kill()
        return finish()

    try:
        # ① 一开始没配
        s = api(base, "/api/ai/status")
        ok("初始 configured=false", s.get("configured") is False, str(s))

        # ① 配上去
        r = api(base, "/api/ai/save", {"api_base": "https://api.deepseek.com",
                                       "model": "deepseek-chat", "api_key": KEY})
        ok("保存返回 ok 且 configured=true", r.get("ok") and r.get("configured"), str(r))
        s = api(base, "/api/ai/status")
        ok("status 报已配置", s.get("configured") is True, str(s))
        ok("status 回显 api_base", s.get("api_base") == "https://api.deepseek.com", str(s))
        ok("status 回显 model", s.get("model") == "deepseek-chat", str(s))

        # ② ★ 密钥不外泄：任何接口的响应原文里都不许出现密钥
        for path in ("/api/ai/status", "/api/config", "/api/health"):
            body = raw(base, path)
            ok("② %s 不含密钥明文" % path, KEY not in body, body[:120])
        ui = raw(base, "/ui/app.js")
        ok("② 前端代码不含密钥明文", KEY not in ui)
        # 落盘文件确实存了（否则上面那些「不含」就成了因为它根本没存）
        saved = json.loads((DATA / "ai.json").read_text(encoding="utf-8"))
        ok("② 密钥确实写进了 data/ai.json", saved.get("api_key") == KEY, str(saved)[:100])

        # ③ 留空＝不改动（改模型不能把密钥抹掉）
        r = api(base, "/api/ai/save", {"model": "deepseek-reasoner"})
        ok("③ 只改模型后仍 configured", r.get("configured") is True, str(r))
        saved = json.loads((DATA / "ai.json").read_text(encoding="utf-8"))
        ok("③ 密钥没被清空", saved.get("api_key") == KEY)
        ok("③ 模型已更新", saved.get("model") == "deepseek-reasoner", saved.get("model"))
        # 空字符串同样按「不改动」处理
        api(base, "/api/ai/save", {"api_key": ""})
        saved = json.loads((DATA / "ai.json").read_text(encoding="utf-8"))
        ok("③ 传空字符串也不清空密钥", saved.get("api_key") == KEY)

        # ⑤ 旧前端兼容：只发 {enabled}
        r = api(base, "/api/ai/save", {"enabled": False})
        ok("⑤ 只发 enabled 不报错", r.get("ok") is True, str(r))
        ok("⑤ enabled 生效", api(base, "/api/ai/status").get("enabled") is False)
        api(base, "/api/ai/save", {"enabled": True})

        # ④ 设置页真的有这些控件，且改动能生效
        from playwright.sync_api import sync_playwright

        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/#settings")
            pg.wait_for_timeout(900)
            for sel in ("#aiBase", "#aiModel", "#aiKey", "#aiSaveBtn"):
                ok("④ 设置页有 %s" % sel, pg.is_visible(sel), "不可见")
            ok("④ 地址已回显", pg.input_value("#aiBase") == "https://api.deepseek.com",
               pg.input_value("#aiBase"))
            ok("④ 模型已回显", pg.input_value("#aiModel") == "deepseek-reasoner",
               pg.input_value("#aiModel"))
            ok("④ ★ 密钥输入框是空的（不回显）", pg.input_value("#aiKey") == "",
               pg.input_value("#aiKey"))
            st = pg.text_content("#aiState") or ""
            ok("④ 通道显示已接通", "接通" in st, st)

            # 在界面上换一个模型并保存
            pg.fill("#aiModel", "deepseek-chat")
            pg.click("#aiSaveBtn")
            pg.wait_for_timeout(1200)
            saved = json.loads((DATA / "ai.json").read_text(encoding="utf-8"))
            ok("④ 界面保存真的落盘", saved.get("model") == "deepseek-chat", saved.get("model"))
            ok("④ 界面保存后密钥仍在", saved.get("api_key") == KEY)
            ok("④ 无 JS 报错", not errs, errs)
            br.close()
    finally:
        try:
            proc.kill()
        except Exception:
            pass

    return finish()


def finish():
    passed = sum(1 for _, c in RESULTS if c)
    total = len(RESULTS)
    print("\n===== 结果: %d/%d 通过 =====" % (passed, total))
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

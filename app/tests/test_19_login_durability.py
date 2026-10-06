# -*- coding: utf-8 -*-
"""自测：让平台登录活得久一点的两条改动

运行：python tests/test_19_login_durability.py
依赖：Playwright + msedge（真开浏览器验 cookie 合并语义）

★ 背景（2026-09-28 实测）：8 个平台里唯一掉登录的是抖音，而它恰好是那天唯一
  发过视频的；其余 6 个没发过的都活着。把登录当时的原始 cookie 装进全新浏览器
  去问平台，平台一样回「用户未登录」—— 说明**是平台在服务端作废了会话**，
  本地 cookie 完好无损。据此改了两处：

  ① 导出/导入登录态时不再用**无头**浏览器
     （抖音能识别无头 —— Dockerfile 里早写过；而这两个函数原来写死 headless=True，
      等于每次登录成功后、每次发布前，都用无头浏览器碰一次真实登录态的 profile）
  ② 发布后把 SAU **刷新过的** cookie 回灌进 profile
     （上游跑完会 `context.storage_state(path=self.account_file)` 写回；
      原项目从不灌回去，profile 永远停在登录那一刻 —— 有去无回）

⚠️ 这两条是**基于证据的推断，不是已证实的因果**。本测试能验的只是
   「改动按设计生效、且没引入新风险」，验不了「平台因此不再作废会话」。

判据：
  ① auth_headless() 的平台默认与环境变量覆盖都对；
  ② 那两个函数里不再写死 headless=True（防回归）；
  ③ ★ 回灌是**合并**不是替换：先灌 A 再灌 B，A 必须还在
     （否则一次回灌就可能把 profile 里别的 cookie 冲掉，比不回灌更糟）；
  ④ publish() 成功路径上确实接了回灌。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

import browser_channel  # noqa: E402
import sau_bridge  # noqa: E402

TMP = APP / "tests" / "_tmp19"
RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def src_of(func_name):
    """取 sau_bridge 里某个函数的源码段（到下一个顶格 def 为止）"""
    body = (APP / "sau_bridge.py").read_text(encoding="utf-8")
    i = body.find("def %s(" % func_name)
    if i < 0:
        return ""
    j = body.find("\ndef ", i + 1)
    return body[i:j] if j > i else body[i:]


def test_headless_switch():
    old = os.environ.get("VP_AUTH_HEADLESS")
    try:
        # 平台默认
        os.environ.pop("VP_AUTH_HEADLESS", None)
        got = browser_channel.auth_headless()
        expect = sys.platform == "win32"
        ok("① 平台默认：Windows=无头 / Linux=有头", got is expect, got)

        # 环境变量覆盖
        os.environ["VP_AUTH_HEADLESS"] = "0"
        ok("① VP_AUTH_HEADLESS=0 → 有头", browser_channel.auth_headless() is False)
        os.environ["VP_AUTH_HEADLESS"] = "1"
        ok("① VP_AUTH_HEADLESS=1 → 无头", browser_channel.auth_headless() is True)
        os.environ["VP_AUTH_HEADLESS"] = "false"
        ok("① VP_AUTH_HEADLESS=false → 有头", browser_channel.auth_headless() is False)
        os.environ["VP_AUTH_HEADLESS"] = ""
        ok("① 空字符串按未设置处理", browser_channel.auth_headless() is expect,
           browser_channel.auth_headless())
    finally:
        if old is None:
            os.environ.pop("VP_AUTH_HEADLESS", None)
        else:
            os.environ["VP_AUTH_HEADLESS"] = old

    # ② 防回归：那两个函数里不许再写死 headless=True
    for fn in ("export_storage_state", "import_storage_state"):
        seg = src_of(fn)
        ok("② %s 存在" % fn, bool(seg))
        ok("② %s 不再写死 headless=True" % fn, "headless=True" not in seg,
           [ln.strip() for ln in seg.splitlines() if "headless" in ln][:2])
        ok("② %s 走 auth_headless()" % fn, "auth_headless()" in seg)

    # ④ publish 成功路径接了回灌
    pseg = src_of("publish")
    ok("④ publish() 里有回灌调用", "import_storage_state(" in pseg)
    ok("④ 回灌带了失败兜底（不影响发布结果）",
       "回灌 cookie 失败" in pseg or "cookie_back" in pseg)


def read_cookies(prof):
    """从 profile 的 Cookies 库里把 cookie 名读出来（真值，不经过浏览器 API）。

    ★ 不用 ctx.cookies()：它对这个场景不可靠（实测读出来是空的，
      而同一时刻库里明明有 cookie）。直接读 SQLite 才作数。
      注意路径随浏览器而变：Edge/新版 Chromium 在 Default/Network/Cookies，
      Linux 容器里 playwright 那份在 Default/Cookies —— 两处都找。
    """
    import sqlite3
    for sub in ("Default/Network/Cookies", "Default/Cookies"):
        db = Path(prof) / sub
        if db.exists():
            try:
                c = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True)
                return {n for (n,) in c.execute("select name from cookies").fetchall()}
            except Exception:  # noqa: BLE001
                pass
    return set()


def test_cookie_merge():
    """③ 回灌必须是**合并**：先灌 A 再灌 B，A 还得在"""
    prof = TMP / "profile"
    if prof.exists():
        shutil.rmtree(prof)
    prof.mkdir(parents=True)

    exp = 1796000000          # 带 expires：真实平台 cookie 基本都是持久的
    fa = TMP / "a.json"
    fa.write_text(json.dumps({"cookies": [
        {"name": "keep_me", "value": "A", "domain": ".example.com", "path": "/", "expires": exp},
        {"name": "shared", "value": "old", "domain": ".example.com", "path": "/", "expires": exp},
    ], "origins": []}), encoding="utf-8")

    fb = TMP / "b.json"
    fb.write_text(json.dumps({"cookies": [
        {"name": "new_one", "value": "B", "domain": ".example.com", "path": "/", "expires": exp},
        {"name": "shared", "value": "new", "domain": ".example.com", "path": "/", "expires": exp},
    ], "origins": []}), encoding="utf-8")

    n1 = sau_bridge.import_storage_state(prof, fa)
    ok("③ 第一次导入返回条数", n1 == 2, n1)
    after_a = read_cookies(prof)
    ok("③ 第一次导入后 keep_me 真的落盘了", "keep_me" in after_a, after_a)
    ok("③ 第一次导入后 shared 也在", "shared" in after_a, after_a)

    n2 = sau_bridge.import_storage_state(prof, fb)
    ok("③ 第二次导入返回条数", n2 == 2, n2)
    after_b = read_cookies(prof)

    ok("③ ★ 合并而非替换：先灌的 keep_me 还在", "keep_me" in after_b, after_b)
    ok("③ 新灌的 new_one 进来了", "new_one" in after_b, after_b)
    ok("③ 同名 cookie 没有被删掉（值是新的）", "shared" in after_b, after_b)


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    test_headless_switch()
    test_cookie_merge()

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

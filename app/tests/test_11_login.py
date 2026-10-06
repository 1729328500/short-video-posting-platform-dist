# -*- coding: utf-8 -*-
"""v1.2 自测：登录态判定重写 + 登录窗自动关闭（释放 profile）

运行：python tests/test_11_login.py
依赖：Playwright + msedge（登录窗与 UI 自动化都要）

背景（2026-09-27 实测）：旧的 URL 判据在 8 个平台上 7 个假阴性、1 个假阳性
（西瓜视频未登录被 302 到 creator.douyin.com，旧逻辑会判成已登录）。
本测试把当时抓到的真实页面正文片段固化成回归用例。
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8967
TMP = APP / "tests" / "_tmp"
SHOTS = APP / "tests" / "shots"

sys.path.insert(0, str(APP))
from platform_login import classify  # noqa: E402

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
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


# ── 2026-09-27 对 8 个平台未登录页的实测正文片段 ───────────────────────
REAL_PAGES = [
    ("抖音未登录", "抖音创作者的一站式创作与服务平台 我是创作者 我是MCN机构 扫码登录 如何扫码 验证码登录 密码登录 获取验证码 登录",
     "https://creator.douyin.com/", "waiting"),
    ("西瓜未登录(被302到抖音)", "抖音创作者的一站式创作与服务平台 我是创作者 扫码登录 如何扫码 验证码登录 密码登录",
     "https://creator.douyin.com/", "waiting"),
    ("快手未登录", "平台介绍 机构入驻 视频上传 快手创作者服务平台 为创作者提供运营管理 立即登录 平台优势",
     "https://cp.kuaishou.com/profile", "waiting"),
    ("头条号未登录", "注册 登录 验证码登录 获取验证码 我已阅读并同意 用户协议 和 隐私政策 登录 扫码登录 请使用今日头条App扫码登录",
     "https://mp.toutiao.com/profile_v4/", "waiting"),
    ("哔哩哔哩未登录", "使用手机 哔哩哔哩客户端 扫码登录 或扫码立即下载 密码登录 短信登录 账号 密码 忘记密码",
     "https://member.bilibili.com/platform/home", "waiting"),
    ("小红书未登录", "创作服务平台 创作百科 加入我们 解锁创作者专属功能 让创作发布数据分析更高效 短信登录 发送验证码 登录即同意",
     "https://creator.xiaohongshu.com/", "waiting"),
    ("视频号登录域", "视频号助手 加热平台 机构管理 特效平台 微信小店 登录",
     "https://channels.weixin.qq.com/login.html", "waiting"),
    ("微博登录域", "搜索 热门推荐 热门榜单 微博热搜 我的 热搜 文娱 社会 科技",
     "https://passport.weibo.com/visitor/visitor?entry=miniblog", "waiting"),
    # ↓ 这一条是 2026-09-27 真机扫码登录后从抖音创作后台抓下来的真实正文片段。
    #   注意它含「作品发布」「发布高清视频」这类带"发布"字样的词，但没有任何登录页特征词 ——
    #   用来防止以后往 LOGIN_MARKERS 里加词时误伤已登录页面。
    ("抖音真机登录后（实测抓取）",
     "作品发布 首页 内容管理 收入变现 创作服务 AI分身 随变 世界书 AI工坊 抖音号：54928795994 关注 0 粉丝指数 0 获赞 "
     "智能创作 作品发布 发布高清视频 支持常用格式 推荐mp4 发布图文 发布全景视频 发布文章 数据中心 统计周期 互动管理 作品评论 私信消息 收入变现",
     "https://creator.douyin.com/creator-micro/home", "on"),
    ("登录后的创作后台", "创作中心 首页 内容管理 数据中心 发布视频 账号设置 退出登录 你已登录",
     "https://cp.kuaishou.com/profile", "on"),
    ("页面加载失败", "", "https://cp.kuaishou.com/", "unknown"),
    ("浏览器错误页", "无法访问此网站 ERR_CONNECTION_TIMED_OUT 请检查网络连接 重新加载",
     "https://creator.douyin.com/", "unknown"),
]


def unit_tests():
    bad = []
    for name, text, url, want in REAL_PAGES:
        st, note, detail = classify(text, url)
        if st != want:
            bad.append((name, st, want))
    ok("判定器：8 平台真实未登录页全部判为 waiting（含西瓜假阳性回归）", not bad, bad)
    ok("判定器：登录后页面判为 on", classify("创作中心 首页 内容管理 数据中心 发布视频 账号设置 退出登录 你已登录",
                                             "https://cp.kuaishou.com/profile")[0] == "on")
    ok("判定器：空白页/错误页不误判为已登录",
       classify("", "https://x.com/")[0] != "on" and classify("无法访问此网站 检查网络连接 重新加载", "https://x.com/")[0] != "on")


def main():
    unit_tests()

    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "data").mkdir(parents=True)
    SHOTS.mkdir(parents=True, exist_ok=True)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(TMP / "data"), "--no-browser",
         "--notify-dry-run", "--mock-login", "douyin,kuaishou"],
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
            pg = br.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda d: d.accept())
            pg.goto(base + "/#accounts")
            pg.wait_for_timeout(900)

            ok("账号页 8 张卡", pg.eval_on_selector_all(".acard", "e=>e.length") == 8)
            body = pg.eval_on_selector("#acGrid", "e=>e.innerText")
            ok("初始 8 个未登录", body.count("未登录") == 8, body.count("未登录"))

            # ── A. 顶栏状态灯：先确认初始是灰的（旧版永远灰，所以还要证明它会变绿）──
            initial_green = pg.eval_on_selector_all("#lamps .dot.ok", "e=>e.length")
            ok("顶栏状态灯初始无绿灯", initial_green == 0, initial_green)

            # ── B. 自动探测：点扫码登录（抖音走 mock）→ 应自动判定已登录 ──
            pg.locator('.acard[data-key="douyin"]').locator(".act-login").click()
            auto_on = False
            for _ in range(70):
                time.sleep(0.5)
                t = pg.locator('.acard[data-key="douyin"]').inner_text()
                if pg.locator('.acard[data-key="douyin"] .badge.ok').count():
                    auto_on = True
                    break
            ok("自动探测：mock 扫码后判定已登录", auto_on,
               pg.locator('.acard[data-key="douyin"]').inner_text())

            # 顶栏灯必须跟着变绿（修 3.3）。
            # 卡片与灯在同一次 loadAccounts 里同步更新，但无头浏览器下两次读取之间
            # 可能被下一轮轮询插进来 —— 所以轮询等待，同时断言两者一致。
            green = 0
            for _ in range(20):
                time.sleep(0.5)
                green = pg.eval_on_selector_all("#lamps .dot.ok", "e=>e.length")
                if green >= 1:
                    break
            ok("顶栏状态灯跟随变绿", green >= 1, green)
            card_txt = pg.locator('.acard[data-key="douyin"]').first.inner_text().replace("\n", "|")
            ok("灯与卡片状态一致（不出现灯绿卡片灰）", bool(pg.locator('.acard[data-key="douyin"] .badge.ok').count()) == (green >= 1),
               "绿=%d 卡片=%s" % (green, card_txt[:60]))

            # ── C. 登录窗必须自动关闭、把 profile 让出来（修 2.1）──
            st_path = TMP / "data" / "browsers" / "douyin.status.json"
            detail = ""
            for _ in range(40):
                time.sleep(0.5)
                try:
                    st = json.loads(st_path.read_text(encoding="utf-8"))
                    detail = st.get("detail", "")
                    if "profile 已释放" in detail:
                        break
                except Exception:
                    pass
            ok("登录窗自动关闭并释放 profile", "profile 已释放" in detail, detail)

            # 状态文件里不该残留 waiting
            st = json.loads(st_path.read_text(encoding="utf-8"))
            ok("状态文件落为 on", st.get("state") == "on", st)
            st.update(account='account-123', account_name='测试账号')
            st_path.write_text(json.dumps(st, ensure_ascii=False), encoding='utf-8')
            confirmed = api(base, '/api/accounts/confirm', {'key': 'douyin'})
            ok('重复确认不会丢失已保存的账号资料', confirmed['status'].get('account') == 'account-123' and confirmed['status'].get('account_name') == '测试账号' and confirmed['status'].get('browser_closed'))

            pg.screenshot(path=str(SHOTS / "30-账号页-自动探测已登录.png"))

            # ── C2. profile 真的腾出来了：用「发布」的方式再打开同一个 profile 必须成功 ──
            #      这是修 2.1 的直接证明 —— 旧版登录窗常驻占着 profile，
            #      发布一开就 profile in use；只看状态文件里的文案是不够的。
            prof = TMP / "data" / "browsers" / "douyin"
            can_publish = False
            why = ""
            try:
                ctx2 = p.chromium.launch_persistent_context(
                    user_data_dir=str(prof), channel="msedge", headless=True,
                    viewport={"width": 1000, "height": 700})
                pg2 = ctx2.pages[0] if ctx2.pages else ctx2.new_page()
                pg2.goto(base + "/mock/logged", wait_until="domcontentloaded", timeout=20000)
                can_publish = True
                ctx2.close()
            except Exception as e:  # noqa: BLE001
                why = str(e)[:160]
            ok("登录窗关闭后发布方能打开同一 profile（修 2.1 的直接证明）", can_publish, why)

            # ── D. 手动确认兜底路径 ──
            pg.locator('.acard[data-key="kuaishou"]').locator(".act-login").click()
            time.sleep(1.5)
            has_btn = pg.locator('.acard[data-key="kuaishou"]').locator(".act-confirm").count()
            ok("等待中显示「我已登录完成」按钮", has_btn == 1, has_btn)
            r = api(base, '/api/accounts/confirm', {'key': 'kuaishou'})
            ok('手动确认等待保存关窗后才返回成功', r.get('ok') and r['status'].get('browser_closed') and r['status'].get('state') == 'on', r)
            bad_status = TMP / 'data/browsers/bilibili.status.json'
            bad_status.write_text(json.dumps({'state': 'error', 'login_mode': 'local', 'note': '保存失败'}), encoding='utf-8')
            rejected = []
            for _ in range(2):
                try:
                    api(base, '/api/accounts/confirm', {'key': 'bilibili'})
                    rejected.append(False)
                except urllib.error.HTTPError as e:
                    rejected.append(e.code == 400)
            ok('重复确认保存失败的本机登录不会强行标为已登录', all(rejected) and json.loads(bad_status.read_text())['state'] == 'error')

            # 直接打接口验证兜底：没有 worker 在跑时应直接落 on
            r = api(base, "/api/accounts/confirm", {"key": "weibo"})
            ok("兜底确认接口可用", r.get("ok") is True, r)
            acc = {a["key"]: a for a in api(base, "/api/accounts")["accounts"]}
            ok("兜底确认后状态为 on", acc["weibo"]["status"] == "on", acc["weibo"])

            # 关闭登录窗口不会退出已完成的登录。
            api(base, "/api/accounts/close", {"key": "weibo"})
            acc = {a["key"]: a for a in api(base, "/api/accounts")["accounts"]}
            ok("关闭窗口后保留已完成的登录", acc["weibo"]["status"] == "on", acc["weibo"])

            # ── E. 共用登录态的平台（西瓜 = 抖音后台）**不能有自己的登录窗** ──
            #    实测踩过：给西瓜配了共用 profile 后，它的登录窗去开抖音的 profile，
            #    而那个正被占着 → Chromium 直接 `Target page, context or browser has been closed`。
            acc = {a["key"]: a for a in api(base, "/api/accounts")["accounts"]}
            ok("西瓜标注为与抖音共用登录态", acc["xigua"].get("shared_with") == "douyin", acc["xigua"])
            ok("西瓜状态跟随抖音",
               acc["xigua"]["status"] == acc["douyin"]["status"],
               (acc["xigua"]["status"], acc["douyin"]["status"]))

            r = api(base, "/api/accounts/login", {"key": "xigua"})
            ok("点西瓜的登录 → 转去开抖音的登录窗（不给自己开）",
               r.get("shared_with") == "douyin" and r.get("ok") is True, r)
            for _ in range(60):
                time.sleep(0.5)
                acc = {a["key"]: a for a in api(base, "/api/accounts")["accounts"]}
                if acc["xigua"]["status"] == "on":
                    break
            ok("抖音登录成功后西瓜自动变已登录", acc["xigua"]["status"] == "on", acc["xigua"])
            api(base, "/api/accounts/close", {"key": "xigua"})
            time.sleep(1)
            acc = {a["key"]: a for a in api(base, "/api/accounts")["accounts"]}
            ok("关闭共用登录窗保留抖音和西瓜的登录", acc["douyin"]["status"] == acc["xigua"]["status"] == "on", acc["douyin"])

            ok("无 JS 报错", not errs, errs)
            br.close()
        proc.terminate(); proc.wait(timeout=8)
        proc = subprocess.Popen([sys.executable, str(APP / 'server.py'), '--port', str(PORT),
            '--data-dir', str(TMP / 'data'), '--no-browser', '--notify-dry-run',
            '--mock-login', 'douyin,kuaishou'], cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
        for _ in range(40):
            try:
                api(base, '/api/health'); break
            except Exception:
                time.sleep(.25)
        acc = {a['key']: a for a in api(base, '/api/accounts')['accounts']}
        ok('工具服务重启后恢复原有平台登录状态', acc['douyin']['status'] == acc['xigua']['status'] == acc['weibo']['status'] == 'on', acc['douyin'])
    finally:
        # 收尾：先让登录窗关掉再停服务。
        # 服务被强杀时子进程不会自动走（Windows 不跑 atexit），登录窗会活成孤儿
        # 并占住 Chromium profile，害得下一次测试/发布「profile in use」。
        for k in ("douyin", "kuaishou", "weibo"):
            try:
                api(base, "/api/accounts/close", {"key": k})
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
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()

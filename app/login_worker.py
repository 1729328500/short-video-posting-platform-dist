# -*- coding: utf-8 -*-
"""登录窗口工作进程（由 server.py 启动/关闭）

职责：打开平台登录页 → 轮询判定登录态 → 写状态文件 → 登录成功后【关窗并退出】。

★★ v1.2 两个关键改动（2026-09-27 实测驱动）：

  1. 判定不再看 URL，改看页面正文的登录页特征词（见 platform_login.py）。
     旧判据在真机上 7/8 假阴性、1/8 假阳性（西瓜视频未登录被 302 到抖音，
     旧逻辑会判成「已登录」）。

  2. 登录成功后【主动关掉浏览器并退出】。
     旧版登录成功后会 `while True` 常驻，一直占着 Chromium 持久化 profile；
     而发布用的正是同一个 profile —— Chromium 同一时刻只允许一个实例占用，
     于是「账号已登录」（发布的前置条件）恰恰导致发布必然失败。
     现在登录确认后关窗，profile 就释放出来给发布用了。

控制通道：server 往 --cmd 指定的文件写 "confirm" / "close"，
         本进程每轮读一次并消费掉（写完即删），用于「我已登录完成」按钮兜底。
"""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from platform_login import classify, probe_account, STABLE_ROUNDS  # noqa: E402
import browser_channel  # noqa: E402
import browser_state  # noqa: E402

GRACE_SECONDS = 2      # 保留短暂写入宽限；成功状态只在浏览器关闭后公布


def write_status(path, obj):
    obj = dict(obj)
    obj["login_mode"] = "local"
    obj["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = None
    try:
        p = Path(path)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=p.parent,
                                         prefix=p.name + ".", suffix=".tmp", delete=False) as f:
            tmp = Path(f.name)
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except OSError:
        return False
    finally:
        if tmp:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def finish_login(ctx, page, args, url, note, detail):
    """Flush the persistent profile and close every tab before reporting success."""
    account, account_name = probe_account(page, args.key)
    info = {"key": args.key, "url": url, "account": account, "account_name": account_name}
    write_status(args.status, dict(info, state="waiting", note="登录已确认，正在保存并自动关闭浏览器…"))
    # Playwright's wait keeps processing browser events while final cookies arrive.
    page.wait_for_timeout(GRACE_SECONDS * 1000)
    browser_state.close(ctx, args.profile, required=True)
    saved = write_status(args.status, dict(info, state="on", note=note,
                         detail=detail + " · 登录窗已关闭，profile 已释放", browser_closed=True,
                         account_checked=True))
    if not saved:
        raise OSError("浏览器已关闭，但保存登录状态失败，请检查数据目录")
    print("[worker %s] closed, profile released account=%s(%s)" % (args.key, account_name, account), flush=True)


def read_cmd(path):
    """读取并消费控制指令（读到即删，避免重复触发）"""
    if not path:
        return ""
    p = Path(path)
    try:
        if not p.exists():
            return ""
        c = p.read_text(encoding="utf-8").strip()
        p.unlink()
        return c
    except OSError:
        return ""


def close_ctx(ctx):
    """优雅关闭浏览器——必须优雅，cookie 要在关闭时落盘"""
    try:
        ctx.close()
    except Exception:
        pass


def server_alive(url):
    """主服务还在不在。

    ★ 为什么要有这个：Windows 上强杀服务进程不会执行 atexit，本进程会活成孤儿，
      而它开着的 Edge 一直占住 Chromium 持久化 profile → 之后发布必然
      「profile in use」失败。实测踩过（调试脚本 kill 服务后留了两个孤儿 Edge）。
      这里定期探一下主服务，连续探不到就自己收摊，孤儿最多活一个探测周期。
    """
    if not url:
        return True
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--url", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--status", required=True)
    ap.add_argument("--cmd", default="")
    ap.add_argument("--health", default="", help="主服务的健康检查地址；探不到就自己退出，避免变孤儿")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--screenshot", default="")
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    write_status(args.status, {"key": args.key, "state": "opening", "url": args.url})

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        write_status(args.status, {"key": args.key, "state": "error",
                                   "note": "未安装 playwright：pip install playwright（%s）" % e})
        sys.exit(1)

    try:
        with sync_playwright() as p:
            with browser_state.persistent_context(p.chromium,
                user_data_dir=args.profile,
                channel=browser_channel.channel(),
                headless=args.headless,
                viewport={"width": 1280, "height": 820},
            ) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass  # 页面加载慢也继续等
                write_status(args.status, {"key": args.key, "state": "waiting", "url": page.url,
                                           "note": "登录窗已打开，等待扫码"})
                if args.screenshot:
                    try:
                        page.screenshot(path=args.screenshot)
                    except Exception:
                        pass

                def read_text():
                    try:
                        return page.evaluate("document.body ? document.body.innerText : ''") or ""
                    except Exception:
                        return ""

                def cur_url():
                    try:
                        return page.url or ""
                    except Exception:
                        return ""

                def scan_all_pages():
                    """把所有打开的页面都判一遍，任一页判为已登录就算登录。

                    ★ 为什么：有些平台扫码后会**新开标签页/弹窗**（微信系尤其典型），
                      只盯 ctx.pages[0] 会一直停在原登录页 —— 实测视频号就是这样：
                      轮询 15 轮 URL 纹丝不动，其实登录可能在另一个页面完成了。
                    返回 (state, note, detail, url, text, page)；优先返回 on。
                    """
                    best = None
                    try:
                        pages = [x for x in list(ctx.pages) if not x.is_closed()]
                    except Exception:
                        pages = [page]
                    for pgx in pages:
                        try:
                            u = pgx.url or ""
                        except Exception:
                            continue
                        try:
                            t = pgx.evaluate("document.body ? document.body.innerText : ''") or ""
                        except Exception:
                            t = ""
                        s, n, d = classify(t, u)
                        if s == "unknown":
                            # ★ 2026-10-06（安全审查 R9）：判不准时用账号接口补一次正向确认。
                            #   只有抖音实现了 probe_account，其余平台立刻返回空 ——
                            #   等价于没调用，不会因为"想要正向确认"而多付网络开销。
                            acc, _an = probe_account(pgx, args.key)
                            if acc:
                                s, n, d = classify(t, u, positive=True)
                        if s == "on":
                            return s, n, d, u, t, pgx
                        if best is None:
                            best = (s, n, d, u, t, pgx)
                    return best or ("unknown", "页面未加载完成", "读不到页面", "", "", None)

                deadline = time.time() + args.timeout
                last_state = ""
                agree = 0
                rounds = 0
                missed = 0

                while time.time() < deadline:
                    time.sleep(2)
                    rounds += 1

                    # 每 ~20 秒探一次主服务；连续 3 次探不到就自己收摊，别当孤儿占着 profile
                    if args.health and rounds % 10 == 0:
                        if server_alive(args.health):
                            missed = 0
                        else:
                            missed += 1
                            print("[worker %s] main server unreachable x%d" % (args.key, missed), flush=True)
                            if missed >= 3:
                                write_status(args.status, {"key": args.key, "state": "closed", "url": "",
                                                           "note": "主服务已退出，登录窗自动关闭"})
                                close_ctx(ctx)
                                return
                    elif args.health and missed:
                        missed = 0

                    cmd = read_cmd(args.cmd)
                    if cmd == "close":
                        write_status(args.status, {"key": args.key, "state": "closed", "url": "",
                                                   "note": "已关闭登录窗"})
                        close_ctx(ctx)
                        return

                    state, note, detail, act_url, _t, active_page = scan_all_pages()
                    url = act_url or cur_url()
                    if not active_page:
                        write_status(args.status, {"key": args.key, "state": "closed", "url": "",
                                                   "note": "窗口已关闭"})
                        return

                    if cmd == "confirm":
                        # 用户手动确认 —— 不参与稳定性判定，直接生效
                        finish_login(ctx, active_page, args, url, "已确认登录（手动）", "你点了「我已登录完成」")
                        return

                    if act_url:
                        url = act_url
                    try:
                        npages = len([x for x in ctx.pages if not x.is_closed()])
                    except Exception:
                        npages = 1
                    print("[worker %s] r%d %s pages=%d url=%s" % (args.key, rounds, state, npages, url), flush=True)

                    if state == "on":
                        # 连续 STABLE_ROUNDS 轮都判已登录才认，避开跳转中间态
                        agree = agree + 1 if last_state == "on" else 1
                        last_state = "on"
                        if agree >= STABLE_ROUNDS:
                            finish_login(ctx, active_page, args, url, note, detail)
                            return
                    else:
                        if last_state != state:
                            agree = 0
                        last_state = state
                    write_status(args.status, {"key": args.key, "state": "waiting" if state == "on" else state,
                                               "url": url, "note": "检测到登录，正在确认…" if state == "on" else note,
                                               "detail": detail})

                write_status(args.status, {"key": args.key, "state": "closed", "url": "",
                                           "note": "等待超时（%d 秒）" % args.timeout})
                close_ctx(ctx)
    except Exception as e:  # noqa: BLE001
        write_status(args.status, {"key": args.key, "state": "error", "url": "",
                                   "note": str(e)[:200]})
        sys.exit(1)


if __name__ == "__main__":
    main()

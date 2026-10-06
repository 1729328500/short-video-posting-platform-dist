# -*- coding: utf-8 -*-
"""数据接口侦察 —— 用已登录 profile 打开页面，把页面**自己发出的 JSON 请求**记下来。

设计文档：`11-数据看板-设计.md` §3.1。

★ 为什么要有它：`data_worker.py` 的每平台实现都要"调平台自己的 JSON 接口"，
  而接口地址与字段名**不能猜**（本项目最忌讳"猜对了也不知道为什么对"）。
  这个工具让我**拿证据**：页面自己会去调那些接口，我把它们抄下来即可。

★ 与 `tools/recon_platform.py` 的分工：那个抓的是**发布适配器**要的 DOM 选择器；
  这个抓的是**数据接口**。别混。

★ Cookie 一律不落盘（见 summarize）：样本可能被粘贴给别人看，凭据不能跟着走。

用法：
    python app/tools/recon_data.py --key bilibili --goto https://member.bilibili.com/platform/upload-manager/video
    python app/tools/recon_data.py --key kuaishou --out /tmp/ks.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import browser_channel  # noqa: E402
from platform_login import LOGIN_URLS  # noqa: E402

# 埋点/上报类（默认不记）——这些占样本 90%，会把真正的数据接口淹掉
NOISE = ("/log/", "/logs/", "report", "beacon", "/monitor", "/metrics/",
         "analytics", "collect?", "sentry", "trace", "/stat?", "pingback")

# 只关心 JSON（或看着像接口的）：其余（图片/JS/CSS）不记
JSON_HINT = ("json", "api", "/x/", "/aweme/", "/janus/", "/web/", "/creator/")


def should_record(url, *, keep_all=False):
    """这个 URL 值不值得记？★ 纯函数（可离线测）。"""
    u = (url or "").lower()
    if not keep_all and any(n in u for n in NOISE):
        return False
    return True


def summarize(rec, limit=500):
    """把一条记录压成"看接口够用"的样子：方法 / URL / 状态 / 顶层键 / 样本片段。

    ★ 纯函数（可离线测）★ **Cookie 一律不落盘**（只留其余请求头，且照样截断）——
      样本会被粘进对话、写进测试 fixture，凭据不能跟着走。
    """
    body = rec.get("body")
    keys = sorted(body.keys()) if isinstance(body, dict) else []
    # ★ 只脱敏 `cookie` 不够（OCR 复查指出）：这些平台的 AJAX 还会带
    #   `authorization` / `x-csrf-token` / `x-xsrf-token` 等凭据 —— 一律不落盘。
    import re as _re
    SENSITIVE = _re.compile(r"cookie|authorization|auth|csrf|xsrf|token|secret|session|pass", _re.I)
    hdrs = {k: v for k, v in (rec.get("req_headers") or {}).items()
            if not SENSITIVE.search(k)}
    sample = json.dumps(body, ensure_ascii=False)[:limit] if body is not None else ""
    return {"method": rec.get("method", "GET"), "url": rec.get("url", ""),
            "status": rec.get("status", 0), "keys": keys,
            "req_headers": hdrs, "sample": sample}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--profile", default="", help="留空＝按数据目录约定推导")
    ap.add_argument("--data-dir", default="", help="留空＝同级 data/")
    ap.add_argument("--goto", default="", help="额外要打开的候选页（逗号分隔）")
    ap.add_argument("--out", default="", help="落盘 JSON（不填就只打印）")
    ap.add_argument("--all", action="store_true", help="连埋点也记（默认过滤）")
    ap.add_argument("--any", action="store_true",
                    help="不管 content-type、什么都记（★ 有的数字不走 JSON 接口，"
                         "或走 gRPC/protobuf —— 反查必须能覆盖到）")
    ap.add_argument("--seconds", type=int, default=12, help="每个页面收集多少秒")
    ap.add_argument("--click", default="", help="加载后依次点击的选择器（逗号分隔）——★ 有的列表要交互才去拉")
    ap.add_argument("--shot", default="", help="每页截一张图到该目录（先确认页面真的渲染出来了）")
    ap.add_argument("--bodies", default="", help="把**完整响应体**逐个落盘到该目录"
                                             "（★ 摘要会截断到 500 字，反查「页面上的某个数字来自哪个接口」必须靠整包）")
    # ★ 默认就是无头，所以**不再给 --headless**（OCR 复查指出：`store_true + default=True`
    #   等于这个旗标传了也没用，是名不副实的开关）。要头就用 `--headful`。
    ap.add_argument("--headful", action="store_true",
                    help="★ 用有头浏览器（容器里有 Xvfb）—— 有的平台对**无头**会给未登录页")
    args = ap.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else (APP_DIR.parent / "data")
    profile = args.profile or str(data_dir / "browsers" / args.key)
    pages = [u for u in (args.goto or "").split(",") if u.strip()]
    if not pages:
        pages = [LOGIN_URLS.get(args.key, "")]
    print("[recon] 平台=%s  打开：%s" % (args.key, " , ".join(pages)))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright")
        return 1

    records = []

    def on_response(resp):
        try:
            u = resp.url
            if not should_record(u, keep_all=args.all):
                return
            ct = (resp.headers or {}).get("content-type", "")
            if not args.any and "json" not in ct and not any(h in u.lower() for h in JSON_HINT):
                return
            try:
                body = resp.json()
            except Exception:                     # noqa: BLE001
                body = resp.body()[:8000].decode("utf-8", "ignore") if args.any else None
                if body is None:
                    return
        except Exception:
            return
        records.append({"method": resp.request.method, "url": resp.url,
                        "status": resp.status,
                        "req_headers": dict(resp.request.headers or {}),
                        "body": body})

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile, channel=browser_channel.channel(),
            headless=(not args.headful),
            viewport={"width": 1440, "height": 900})
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        pg.on("response", on_response)
        for u in pages:
            if not u:
                continue
            try:
                pg.goto(u, wait_until="domcontentloaded", timeout=60000)
                time.sleep(max(3, args.seconds))
            except Exception as e:  # noqa: BLE001
                print("[recon] 打开失败 %s：%s" % (u, str(e)[:80]))
            # ★ 有的列表接口**只有交互之后才去拉**（B站实测：光打开页面 0 命中）
            for sel in [x for x in (args.click or "").split(",") if x.strip()]:
                try:
                    pg.click(sel, timeout=8000)
                    print("[recon] 已点击 %s" % sel)
                    time.sleep(max(3, args.seconds))
                except Exception as e:            # noqa: BLE001
                    print("[recon] 点击失败 %s：%s" % (sel, str(e)[:70]))
            if args.shot:
                try:
                    Path(args.shot).mkdir(parents=True, exist_ok=True)
                    pg.screenshot(path=str(Path(args.shot) / ("p%d.png" % len(records))))
                    print("[recon] 截图 %s/p%d.png" % (args.shot, len(records)))
                except Exception as e:            # noqa: BLE001
                    print("[recon] 截图失败：%s" % str(e)[:70])
        try:
            ctx.close()
        except Exception:
            pass

    if args.bodies:
        bdir = Path(args.bodies)
        bdir.mkdir(parents=True, exist_ok=True)
        for i, r in enumerate(records):
            try:
                (bdir / ("%03d.json" % i)).write_text(
                    json.dumps({"url": r["url"], "body": r["body"]}, ensure_ascii=False),
                    encoding="utf-8")
            except Exception:                     # noqa: BLE001
                pass
        print("[recon] 完整响应体已落盘 %s（%d 个）" % (args.bodies, len(records)))
    out = [summarize(r) for r in records]
    # 去重（同 URL 去参数后只留第一条，带样本）
    seen, uniq = set(), []
    for r in out:
        k = r["url"].split("?")[0]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    for r in uniq:
        print("\n[%s] %s  → %s" % (r["method"], r["status"], r["url"][:150]))
        print("      顶层键：%s" % r["keys"])
        print("      样本：%s" % r["sample"][:240])
    print("\n[recon] 共 %d 条（去重后 %d）" % (len(records), len(uniq)))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)   # ★ 别等收集完了才 FileNotFoundError
        Path(args.out).write_text(json.dumps(uniq, ensure_ascii=False, indent=1), encoding="utf-8")
        print("[recon] 已落盘 %s" % args.out)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

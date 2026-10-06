# -*- coding: utf-8 -*-
"""平台适配一键侦察 —— 把《08-平台适配策略》里那 8 项一次抓出来。

用法：
    # 用该平台已登录的 profile 打开它的发布页，导出可直接粘进 publish_adapters.json 的片段
    python app/tools/recon_platform.py toutiao
    python app/tools/recon_platform.py xigua --url https://xxx/upload

前提：先去账号页给该平台扫码登录（登录态在 data/browsers/<平台>；若该平台声明了
      profile_key，则用共用那个 profile）。

输出 8 项对应《08-平台适配策略》第二节：
    ① 入口 URL      ② 登录判据（后台路径）  ③ 上传入口  ④ 标题
    ⑤ 正文          ⑥ 话题下拉            ⑦ 提交按钮  ⑧ 成功落点（需手动发一次才知道）
"""
import argparse
import json
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from platform_login import classify  # noqa: E402
import browser_channel  # noqa: E402

AD_PATH = APP / "publish_adapters.json"


def load_ad(key):
    try:
        d = json.loads(AD_PATH.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    plats = d.get("platforms") or {}
    p = dict(plats.get(key) or {})
    parent = p.get("inherits")
    if parent and parent in plats:
        merged = dict(plats[parent] or {})
        merged.update(p)
        return merged
    return p


def probe(pg):
    return pg.evaluate(r"""() => {
      const sel = e => {
        if (e.id) return '#' + e.id;
        const ph = e.getAttribute && e.getAttribute('placeholder');
        if (ph) return e.tagName.toLowerCase() + "[placeholder*='" + ph.slice(0,12) + "']";
        const c = (e.className||'').toString().trim().split(/\s+/).filter(Boolean).slice(0,2).join('.');
        return e.tagName.toLowerCase() + (c ? '.' + c : '');
      };
      const q = s => [...document.querySelectorAll(s)];
      return {
        files: q('input[type=file]').map(e => ({sel: sel(e), accept: (e.accept||'').slice(0,60), 可见: e.offsetParent!==null})),
        texts: q('input[type=text],input:not([type])').slice(0,10).map(e => ({sel: sel(e), ph: e.placeholder})),
        areas: q('textarea').slice(0,6).map(e => ({sel: sel(e), ph: e.placeholder})),
        eds:   q('[contenteditable=true]').slice(0,6).map(e => ({sel: sel(e), cls: (e.className||'').slice(0,60)})),
        btns:  q('button').map(e => e.innerText.trim()).filter(t => t && t.length < 12).slice(0,25),
      };
    }""")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key", help="平台 key：douyin/channels/kuaishou/xhs/weibo/toutiao/bilibili/xigua")
    ap.add_argument("--url", default="", help="覆盖适配器里的发布页地址")
    ap.add_argument("--data-dir", default=str(APP / "data"))
    ap.add_argument("--headless", action="store_true", help="默认有头，方便你顺手登录")
    ap.add_argument("--wait", type=int, default=8)
    args = ap.parse_args()

    ad = load_ad(args.key)
    if not ad:
        print("适配器里没有平台：%s" % args.key)
        return 1
    url = args.url or ad.get("publish_url") or ""
    if not url:
        print("没有发布页地址，请用 --url 指定")
        return 1

    prof_key = ad.get("profile_key") or args.key
    prof = Path(args.data_dir) / "browsers" / prof_key
    print("平台      : %s（%s）" % (args.key, ad.get("name", args.key)))
    print("登录 profile: %s  %s" % (prof, "（存在）" if prof.exists() else "（不存在，先扫码登录）"))
    print("发布页    : %s" % url)
    print("继承自    : %s" % (ad.get("inherits") or "——"))
    print("-" * 72)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright：pip install playwright")
        return 1

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(prof), channel=browser_channel.channel(), headless=args.headless,
            viewport={"width": 1440, "height": 900})
        pg = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            pg.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("  打开失败:", str(e)[:100])
        time.sleep(args.wait)

        txt = ""
        try:
            txt = pg.evaluate("document.body ? document.body.innerText : ''") or ""
        except Exception:
            pass
        st, note, detail = classify(txt, pg.url)

        print("① 入口 URL      : %s" % pg.url)
        print("② 登录判据      : %s  | %s  | %s" % (st, note, detail))
        if st != "on":
            print("   ⚠️ 当前判为未登录 —— 先在账号页扫码登录再跑本工具")
        print()
        info = probe(pg)
        print("③ 上传入口      : %s" % json.dumps(info["files"], ensure_ascii=False))
        print("④ 标题          : %s" % json.dumps(info["texts"], ensure_ascii=False))
        print("⑤ 正文          : %s" % json.dumps(info["eds"] + info["areas"], ensure_ascii=False))
        print("⑦ 页面按钮      : %s" % json.dumps(info["btns"], ensure_ascii=False))
        print()
        print("   页面正文开头  : %s" % txt[:180].replace("\n", " / "))
        # 截图落在数据目录里，别写进源码树/交付目录
        shot = Path(args.data_dir) / ("recon_%s.png" % args.key)
        try:
            pg.screenshot(path=str(shot), full_page=True)
            print("   截图          : %s" % shot)
        except Exception:
            pass
        ctx.close()

    print("-" * 72)
    print("⑧ 成功落点：需要**手动发一条**才拿得到 —— 提交后看地址栏变成什么，填进 manage_url")
    print()
    print("把下面这段按实际情况改好，粘进 app/publish_adapters.json 的 platforms 里：")
    print(json.dumps({
        args.key: {
            "name": ad.get("name", args.key),
            "calibrated": True,
            "calibrated_at": time.strftime("%Y-%m-%d 真机校准"),
            "publish_url": pg.url if 'pg' in dir() else url,
            "file_input": "input[type=file][accept*='video']",
            "title_input": "input[placeholder*='标题']",
            "body_editor": "[contenteditable=true]",
            "submit_text": "发布",
            "manage_url": "（提交后地址栏的地址）",
            "verify_dialog_hints": ["短信验证码", "请输入验证码", "获取验证码"],
        }
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

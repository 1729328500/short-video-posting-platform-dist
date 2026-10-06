# -*- coding: utf-8 -*-
"""探出各平台当前登录的账号，写回状态文件。

为什么需要：「单账号日更上限」要按**账号**算。登录窗在判定登录成功时已经会探一次，
但历史登录（本功能上线前扫的码）没有账号信息 —— 这个脚本负责补齐，
让用户不必为了拿到账号 ID 重新扫码。

用法：
    python app/tools/probe_accounts.py --data-dir <数据目录> [--key douyin]

★ 退出码（2026-09-30 起）：0 = 全部平台探测成功；1 = 环境问题（如没装 playwright）；
  2 = **有平台探测失败**。旧版无论失败多少个平台都 `return 0`，服务端据此把核验记成
  「完成」—— 8 个平台全探测失败也报成功（本项目最忌的"看起来成功"，实测复现过）。
"""
import argparse
import json
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

import browser_channel  # noqa: E402
import browser_state  # noqa: E402

from platform_login import LOGIN_URLS, classify, probe_account, verify_state  # noqa: E402

# ★ 2026-09-29：登录态**核验**对所有平台都可用（判据 classify 本来就是通用的，
#   登录窗用的就是它）；这里限制的只是"能不能探到**账号身份**"。
#   所以 SUPPORTED = 全部平台 —— 探不到身份不影响"核验登录态"这个用途。
SUPPORTED = tuple(LOGIN_URLS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--key", default="", help="只探某个平台；留空＝所有支持的平台")
    ap.add_argument("--verify", action="store_true",
                    help="无视状态文件，真开浏览器核一遍登录态并据实写回。"
                         "★ 状态文件是「上一次操作的记录」，不是事实 —— "
                         "被写歪（比如取消登录把已登录的账号标成了未登录）时用它复原。")
    args = ap.parse_args()

    data = Path(args.data_dir)
    keys = [args.key] if args.key else list(SUPPORTED)
    keys = [k for k in keys if k in SUPPORTED]
    if not keys:
        print("没有可探测的平台（已实现的：%s）" % "、".join(SUPPORTED))
        return 0

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("未安装 playwright")
        return 1

    failed, probed, unverified = [], 0, []
    with sync_playwright() as p:
        for key in keys:
            sp = data / "browsers" / ("%s.status.json" % key)
            try:
                st = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
            except Exception:
                st = {}
            if not args.verify:
                if st.get("state") != "on":
                    print("%-9s 未登录，跳过" % key)
                    continue
                if st.get("account"):
                    print("%-9s 已有账号 %s(%s)，跳过" % (key, st.get("account_name"), st.get("account")))
                    continue
                if st.get("account_checked"):
                    continue
            prof = data / "browsers" / key
            ctx = None
            try:
                with browser_state.persistent_context(p.chromium,
                    user_data_dir=str(prof), channel=browser_channel.channel(), headless=True,
                    viewport={"width": 1280, "height": 820}) as ctx:
                    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                    url = LOGIN_URLS.get(key, "")
                    if not url:
                        print("%-9s 没配入口 URL，跳过" % key)
                        continue
                    # ★ 打开各平台**自己的**后台入口（原来写死抖音 —— 那是"只有抖音能用"的真因）
                    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(5)
                    an, anm = probe_account(pg, key)
                    st["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    if args.verify:
                        # ★ --verify：以**实际探测结果**为准写回状态文件。
                        #   探测方式就是登录窗用的那套判据（看正文特征词，不看 URL），
                        #   所以它给出的结论和「登录窗认不认」是一致的。
                        txt = pg.evaluate("document.body ? document.body.innerText : ''") or ""
                        cls, note, detail = classify(txt, pg.url)
                        if cls == "unknown" and an:
                            # 账号接口探到了身份 = 正向确认（只有抖音实现了）
                            cls, note, detail = classify(txt, pg.url, positive=True)
                        want = verify_state(cls)
                        if want is None:
                            # ★ 2026-10-06（安全审查 R9）：判不准**保留原状态**，不写 off ——
                            #   写 off 会把已登录账号标成未登录，用户点发布直接被拒。
                            #   但仍要算"未能确认"（沿用 2026-09-30 定的"失败要算账"）：
                            #   退出码必须反映真相，不能回到"假完成"。
                            unverified.append("%s（%s）" % (key, note))
                        else:
                            st["state"] = want
                        st["url"] = pg.url
                        st["note"] = "已核验登录态：%s" % note
                        st["detail"] = detail
                        # ★ 2026-09-30：单独记「最后核验时间」。数据库的 updated_at 只在
                        #   状态/文案**变化**时才动，不能当"最后核验时间"用 —— 核验过、
                        #   结果没变时它还停在昨天，用户点了核验看不出到底跑没跑。
                        st["verified_at"] = st["at"]
                        # ★ 只有真探到才写（`and an`）：非抖音平台探不到身份，
                        #   少了这个判断，一次核验就会把库里已有的账号 ID 抹成空。
                        if cls == "on" and an:
                            st["account"] = an
                            st["account_name"] = anm
                        sp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
                        probed += 1
                        print("%-9s 核验=%s（%s）账号=%s(%s)"
                              % (key, cls, note, anm, an))
                        continue
                    if an:
                        st["account"] = an
                        st["account_name"] = anm
                        sp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
                        print("%-9s ✓ 识别到账号 %s(%s)" % (key, anm, an))
                    else:
                        print("%-9s 没探到账号信息" % key)
            except Exception as e:  # noqa: BLE001
                failed.append(key)          # ★ 失败要算账，不许吞（旧版就是吞在这里）
                print("%-9s 探测失败：%s" % (key, str(e)[:100]))
    if failed:
        # ★ 2026-09-30 实测复现：把 profile 目录堵死 → 8 个平台全失败，旧版仍 `return 0`，
        #   服务端据此记「核验完成，状态已按实写回」—— 一句谎话。返回码必须反映真相，
        #   最后一行留给服务端当界面文案（点名失败的平台）。
        print("核验失败：%d/%d 个平台探测失败：%s" % (len(failed), len(keys), "、".join(failed)))
        return 2
    if unverified:
        # ★ 2026-10-06（安全审查 R9）：判不准不算"核验完成"（同一套"失败要算账"的原则）。
        #   但文案必须说清是"未能确认、已保留原状态"，不是"失败" ——
        #   这些账号很可能好着，只是页面特征对不上。
        print("核验未能确认：%d/%d 个平台（%s）—— 已保留原状态，请人工确认"
              % (len(unverified), len(keys), "、".join(unverified)))
        return 2
    if args.verify and probed:
        print("核验完成：%d 个平台已核验" % probed)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

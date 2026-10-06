# -*- coding: utf-8 -*-
"""自测：用户设的封面要**跟着发布走**

运行：python tests/test_23_cover_to_publish.py

★ 背景（2026-09-28 用户实测）：微博发布直接失败 ——
    `微博视频发布必须提供封面图（--thumbnail）`。
  查下来：**封面功能早就做好了**（素材页可以截帧或上传图片，落盘 data/covers/，
  数据库里有 videos.cover 字段），但**发布链路上从来没把它传出去** ——
  `sau_bridge.publish()` 只传 title/file_path/tags/publish_date/account_file/headless/desc。
  微博是唯一**硬性要求**封面的平台（不带就 raise），所以它必失败；
  头条号同理（适配器那条路，之前连封面这一步都没有）。

判据：
  ① 封面参数名按平台分派 —— 抖音跟别人**不一样**，传错名字是 TypeError；
  ② run_real_job 在素材有封面时，确实把 covers/ 里的路径传给了发布进程；
  ③ 没设封面时传空串（上游会自行跳过，不该报错）；
  ④ publish_worker 真的收 `--thumbnail` 这个参数；
  ⑤ 适配器没配 cover_trigger 时整步跳过（对已经能发的平台零影响）。
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

import sau_bridge  # noqa: E402

TMP = APP / "tests" / "_tmp23"
RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def test_kwarg_mapping():
    ok("① 抖音用 thumbnail_portrait_path",
       sau_bridge.thumbnail_kwarg("douyin") == "thumbnail_portrait_path",
       sau_bridge.thumbnail_kwarg("douyin"))
    for k in ("channels", "kuaishou", "xhs", "weibo"):
        ok("① %s 用 thumbnail_path" % k,
           sau_bridge.thumbnail_kwarg(k) == "thumbnail_path",
           sau_bridge.thumbnail_kwarg(k))
    ok("① 未知平台退回 thumbnail_path",
       sau_bridge.thumbnail_kwarg("nope") == "thumbnail_path")


def _capture_args(has_cover):
    """造一条真实任务，用假 Popen 截获 run_real_job 拼出来的命令行"""
    import server

    d = TMP / ("withcover" if has_cover else "nocover")
    if d.exists():
        shutil.rmtree(d)
    (d / "covers").mkdir(parents=True)
    (d / "videos").mkdir(parents=True)
    (d / "publish").mkdir(parents=True)
    (d / "videos" / "v.mp4").write_bytes(b"\x00" * 1024)
    if has_cover:
        (d / "covers" / "c.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)

    store = server.Store(d)
    with sqlite3.connect(d / "app.db") as c:
        c.execute("INSERT INTO videos(name,stored,size,added_at,cover) VALUES(?,?,?,?,?)",
                  ("t", "v.mp4", 1024, "2026-09-28 00:00:00", "c.png" if has_cover else None))
        vid = c.execute("SELECT id FROM videos").fetchone()[0]
    batch = store.create_publish_batch(vid, ["weibo"], {}, "now", "", True)
    job = store.get_batch(batch)[0]

    captured = {}
    real_popen = server.subprocess.Popen

    class FakeProc:
        def poll(self):
            return 0

    def fake_popen(args, **kw):
        captured["args"] = list(args)
        return FakeProc()

    server.subprocess.Popen = fake_popen
    try:
        server.run_real_job(store, job)
    finally:
        server.subprocess.Popen = real_popen
    return captured.get("args") or []


def test_run_real_job_args():
    args = _capture_args(True)
    ok("② 命令行里有 --thumbnail", "--thumbnail" in args, " ".join(args)[:200])
    if "--thumbnail" in args:
        val = args[args.index("--thumbnail") + 1]
        ok("② ★ 传的是 covers/ 里那张图",
           val.replace("\\", "/").endswith("/covers/c.png"), val)

    args2 = _capture_args(False)
    if "--thumbnail" in args2:
        ok("③ 没设封面时传空串", args2[args2.index("--thumbnail") + 1] == "",
           repr(args2[args2.index("--thumbnail") + 1]))
    else:
        ok("③ 没设封面时传空串", False, "命令行里没有 --thumbnail")


def test_worker_arg_and_adapter():
    out = subprocess.run([sys.executable, str(APP / "publish_worker.py"), "--help"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok("④ publish_worker 收 --thumbnail", "--thumbnail" in (out.stdout or ""),
       (out.stdout or "")[:120])

    ad = json.loads((APP / "publish_adapters.json").read_text(encoding="utf-8"))
    de = ad.get("defaults") or {}
    ok("⑤ 适配器有 cover_trigger 键", "cover_trigger" in de, list(de)[:8])
    ok("⑤ ★ 默认留空 = 整步跳过（不影响已能发的平台）", de.get("cover_trigger") == "",
       repr(de.get("cover_trigger")))
    ok("⑤ 有 cover_file_input 兜底选择器",
       bool(de.get("cover_file_input")), repr(de.get("cover_file_input")))

    src = (APP / "publish_worker.py").read_text(encoding="utf-8")
    ok("⑤ 适配器流程里确实有封面这一步", "封面已上传" in src)
    ok("⑤ 封面失败不中断发布", "封面上传出错（继续）" in src or "没找到封面入口" in src)

    # ★ 2026-09-28 用户实测「头条/B站封面没自动填入」后的改法：
    #   不再要求逐平台配 cover_trigger，改成锚在「封面」二字上逐个试 + 验。
    ok("⑤ 有通用的封面入口候选（不依赖逐平台标定）", "DEFAULT_COVER_TRIGGERS" in src)
    ok("⑤ 通用候选锚在「封面」文案上", "设置封面" in src and "更换封面" in src)
    ok("⑤ ★ 封面文件框只认图片型 accept（不兜底成裸 file input）",
       "COVER_FILE_INPUTS" in src and "input[type=file][accept*='image']" in src)
    ok("⑤ ★ 停了提交前会落盘现场（供标定）", "dump_hold_snapshot" in src)

    # ★ 顺序：主表单上的入口（添加/设置封面）要先于弹层内的「上传封面」。
    #   实测 B站：添加封面在主表单、上传封面在 bcc-dialog 里 —— 顺序反了会先点弹层里那个。
    i_main = src.find('"text=添加封面"')
    i_dlg = src.find('"text=上传封面"')
    ok("⑤ ★ 主表单入口排在弹层内入口前面",
       0 < i_main < i_dlg, "添加封面@%d 上传封面@%d" % (i_main, i_dlg))

    # ★ 判定「弹层开没开」不能用「找得到图片框」—— 那个框在弹层关着时也在 DOM 里。
    ok("⑤ ★ 有弹层可见性判据（不能只靠找得到文件框）", "DIALOG_HINTS" in src)
    # ★ 2026-09-29 改判据：原来是"在 publish_worker.py 的源码里找 dialog/layer/modal 三个词"，
    #   那测的是**词在不在文件里**，不是"判据覆盖不覆盖"。DIALOG_HINTS 改成引用
    #   dialog_settle.DEFAULT_HINTS（单一事实来源，见 10-弹窗收尾-设计.md §2.1）之后，
    #   字面量不在本文件里了 → 老判据会假失败。改成查**真正的选择器表**。
    import dialog_settle as _ds
    _hints = " ".join(_ds.DEFAULT_HINTS)
    ok("⑤ 弹层判据覆盖 dialog/layer/modal/popup",
       all(k in _hints for k in ("dialog", "layer", "modal", "popup")), _ds.DEFAULT_HINTS)
    ok("⑤ 没确认弹层时会如实标注（不谎报成功）", "未确认弹层" in src)


def test_nav_entry():
    """⑥ 左导航下拉里的发布入口（2026-09-28 用户实测：头条号/B站识别不到）"""
    ad = json.loads((APP / "publish_adapters.json").read_text(encoding="utf-8"))
    plats = ad.get("platforms") or {}

    for k, name in (("toutiao", "头条号"), ("bilibili", "哔哩哔哩")):
        v = plats.get(k) or {}
        ok("⑥ %s 配了 entry_url" % name, bool(v.get("entry_url")), repr(v.get("entry_url")))
        ok("⑥ %s 配了 entry_hover（下拉要先悬停展开）" % name,
           bool(v.get("entry_hover")), repr(v.get("entry_hover")))
        ok("⑥ %s 配了 entry_click" % name, bool(v.get("entry_click")), repr(v.get("entry_click")))

    # B站的 accept 是**扩展名**不是 MIME，默认的 accept*='video' 一个都匹配不上
    bi = (plats.get("bilibili") or {}).get("file_input") or ""
    ok("⑥ ★ B站 file_input 用扩展名写法（它的 accept 不是 MIME）",
       ".mp4" in bi, bi)

    src = (APP / "publish_worker.py").read_text(encoding="utf-8")
    ok("⑥ publish_worker 里有走导航入口的代码", "已从导航入口进入发布页" in src)
    # ★ 入口那步加了重试（B站实测：SPA 导航栏渲染晚，只试一次会撞上元素还不存在）
    ok("⑥ ★ 入口失败会重试（不是只试一次）", "for attempt in range(4)" in src)
    ok("⑥ ★ 入口彻底失败仍回退到直接开发布页（老路子没被弄没）",
       "走导航入口失败" in src and "page.goto(publish_url" in src)
    ok("⑥ 没配 entry_url 时仍走 publish_url", "ad.get(\"entry_url\") or publish_url" in src)


def test_publish_page_ui():
    """⑦ 发布台上要有封面入口（用户 2026-09-28：入口只在素材库，发布台没有）"""
    import urllib.request
    import urllib.parse

    d = TMP / "ui"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)

    logf = open(d / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, "-u", str(APP / "server.py"), "--port", "8988",
         "--data-dir", str(d), "--no-browser", "--notify-dry-run"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:8988"
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            up = True
            break
        except Exception:
            time.sleep(0.5)
    ok("⑦ 测试服务起来了", up)
    if not up:
        proc.kill()
        return
    try:
        req = urllib.request.Request(
            base + "/api/videos/upload?name=" + urllib.parse.quote("封面UI.mp4"),
            data=b"\x00" * 4096, method="POST")
        vid = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())["video"]["id"]

        from playwright.sync_api import sync_playwright
        errs = []
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True)
            pg = br.new_page(viewport={"width": 1280, "height": 900})
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("dialog", lambda dd: dd.accept())
            pg.goto(base + "/#publish")
            pg.wait_for_timeout(1200)
            ok("⑦ 发布台有封面按钮", pg.is_visible("#pubCoverBtn"), "看不见")
            ok("⑦ 有封面缩略图位", pg.locator("#pubCoverThumb").count() == 1)

            # 没选视频时按钮是禁用的（提示「先从上面选一条视频」）
            hint = pg.text_content("#pubCoverHint") or ""
            ok("⑦ 未选视频时给出提示", bool(hint.strip()), repr(hint[:40]))
            ok("⑦ 未选视频时按钮禁用", pg.is_disabled("#pubCoverBtn"), "竟然可点")

            # 选上视频 → 按钮可用
            pg.select_option("#videoSel", str(vid))
            pg.wait_for_timeout(500)
            ok("⑦ 选了视频后按钮可用", not pg.is_disabled("#pubCoverBtn"), "还是禁用")
            hint = pg.text_content("#pubCoverHint") or ""
            ok("⑦ 未设封面时提示会说明可以不设", "封面" in hint, repr(hint[:60]))

            # 点它 → 应该弹出素材库那个封面弹层（截帧 / 上传图片）
            pg.click("#pubCoverBtn")
            pg.wait_for_timeout(800)
            ok("⑦ 点开是封面弹层", pg.is_visible("#mask.open"), "弹层没出现")
            body = pg.eval_on_selector("#mBody", "e=>e.innerText")
            ok("⑦ 弹层里两种方式都在（截帧 / 上传）",
               "截取当前帧" in body or "上传" in body, body[:80])

            # ★ 真上传一张图片当封面 —— 用户反馈「只能传视频不能传图片」，
            #   这里把整条图片链路（选文件 → POST /api/videos/cover → 落盘）跑通，
            #   别只看代码里有没有这个按钮。
            img = d / "cover.png"
            img.write_bytes(bytes.fromhex(
                "89504e470d0a1a0a0000000d494844520000000100000001080600000"
                "01f15c4890000000a49444154789c6300010000050001a5f645000000"
                "0049454e44ae426082"))
            pg.set_input_files("#imageInput", str(img))
            pg.wait_for_timeout(1500)

            j = json.loads(urllib.request.urlopen(
                base + "/api/videos", timeout=10).read().decode())
            v2 = [x for x in j["videos"] if x["id"] == vid][0]
            ok("⑦ ★ 图片能当封面存下来", bool(v2.get("cover")), repr(v2.get("cover")))
            if v2.get("cover"):
                ok("⑦ 是 png", str(v2["cover"]).endswith(".png"), v2["cover"])
                # 缩略图在发布台上跟着更新了没
                pg.select_option("#videoSel", str(vid))
                pg.wait_for_timeout(600)
                ok("⑦ 发布台缩略图已显示",
                   pg.is_visible("#pubCoverThumb"), "缩略图没出来")
            ok("⑦ 无 JS 报错", not errs, errs)
            br.close()
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    test_kwarg_mapping()
    test_run_real_job_args()
    test_worker_arg_and_adapter()
    test_nav_entry()
    test_publish_page_ui()
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

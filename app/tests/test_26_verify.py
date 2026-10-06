# -*- coding: utf-8 -*-
"""自测：①核验登录态 ②SAU 独立核验 —— 两条都是「别再自欺」的机制。

运行：cd app && python -X utf8 tests/test_26_verify.py

★ 背景（2026-09-29 真机批次 14，用户现场核对发现）：
  · **状态文件≠事实**：账号页全是"已登录"，可视频号一发布就报 cookie 失效
    （交接文档 §3.2 / 坑 2）。⇒ 加"核验登录态"：真开一次浏览器、用**和登录窗同一套判据**
    核，并按实写回（工具早就有：tools/probe_accounts.py --verify，只是入口写死了抖音）。
  · **上传器说成功 ≠ 发出去了**：抖音 11:18:23 点发布 → **11:18:24 就报成功**（上游只看
    URL 跳没跳），而页面提示"服务器问题"、创作中心里没有新视频。⇒ 加 SAU 独立核验：
    另开一次浏览器去作品管理页**查标题在不在列表里**。

判据（全是离线可验的）：
  1  LOGIN_URLS 只有一份：platform_login 是唯一来源，server 引用的是同一份
  2  核验入口对**全部 8 个平台**开放（原来 SUPPORTED 只放抖音）
  3  probe_accounts 用的是各平台自己的入口 URL（不是写死的抖音）
  4  核验探不到账号身份时**不许把已存的账号抹掉**
  5  ★ 三个护栏：正在发布 / 登录窗还开着 / 已在核验中 → 一律拒绝（会抢同一个 profile）
  6  /api/accounts 会把核验进度带给界面（否则界面无法显示"核验中"）
  7  verify_published：没配 manage_url 就**不猜**（返回空，口径保持"未独立核验"）
  8  SAU 成功分支真的调了 verify_published
  9  抖音有 manage_url（有证据）；其余 SAU 平台**留空**（不猜）
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


class _FakeDecLoc:
    """够 _select_declaration 用的假 locator。"""

    def __init__(self, page, sel, idx=0):
        self.page, self.sel, self.idx = page, sel, idx

    @property
    def first(self):
        return self

    def count(self):
        if "bcc-select-input-wrap" in self.sel or "bcc-option" in self.sel:
            return 1
        if self.sel.endswith("input") or "input" in self.sel:
            return 1
        return 0

    def is_visible(self):
        return self.count() > 0

    def filter(self, **kw):
        """新定位用 `locator("li.bcc-option").filter(has_text=re.compile(...))` —— 假对象跟上。"""
        return self

    def click(self, timeout=None):
        self.page.clicked.append(self.sel)
        if "bcc-option" in self.sel and not self.page.dead_option:
            self.page.value = self.page.option_text      # 点中选项 → 输入框显示该文案

    def input_value(self, timeout=None):
        return self.page.value


class _FakeDecPage:
    """假 page：模拟 B站「创作声明」那个 bcc-select 下拉。"""

    def __init__(self, dead_option=False):
        self.value = ""
        self.clicked = []
        self.option_text = "含AI生成内容"
        self.dead_option = dead_option

    def locator(self, sel):
        return _FakeDecLoc(self, sel)

def main():
    import platform_login as pl
    import server
    import publish_worker as pw

    # 1 单一事实来源
    ok("1 server 的 LOGIN_URLS 就是 platform_login 那份（同一对象/内容）",
       server.LOGIN_URLS == pl.LOGIN_URLS and len(pl.LOGIN_URLS) == 8,
       len(pl.LOGIN_URLS))

    # 2 核验入口覆盖全部平台
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("pa", str(APP / "tools" / "probe_accounts.py"))
        pa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(pa)
        ok("2 probe_accounts.SUPPORTED 覆盖全部 8 个平台", tuple(pa.SUPPORTED) == tuple(pl.LOGIN_URLS),
           getattr(pa, "SUPPORTED", None))
    except Exception as e:            # noqa: BLE001
        ok("2 probe_accounts 可导入", False, str(e)[:80])

    # 3/4 源码层（这两条是"接线"性质，行为要真开浏览器才能验）
    pa_src = (APP / "tools" / "probe_accounts.py").read_text(encoding="utf-8")
    ok("3 用各平台自己的入口 URL（不再写死抖音）",
       "LOGIN_URLS.get(key" in pa_src and "creator.douyin.com/creator-micro/home" not in pa_src)
    ok("4 探不到身份时不抹掉已存账号（cls==on and an）", 'cls == "on" and an' in pa_src)

    # 5 三个护栏（行为级：护栏在读状态之前就该拒绝，所以 store 传 None 也能测）
    held = server.PUB_RUN_LOCK.acquire(blocking=False)
    try:
        okv, err = server.verify_login_state(None, ["douyin"])
        ok("5a 正在发布时拒绝并说明原因",
           (okv is False) and ("发布" in err), (okv, err))
    finally:
        if held:
            server.PUB_RUN_LOCK.release()

    server.VERIFY_STATE["douyin"] = {"state": "running", "at": "00:00:00", "note": ""}
    okv2, err2 = server.verify_login_state(None, ["douyin"])
    ok("5b 已在核验中 → 拒绝", (okv2 is False) and ("核验中" in err2), (okv2, err2))
    server.VERIFY_STATE.pop("douyin", None)

    class FakeStore:
        data_dir = Path(".")

    _orig_alive = server.worker_alive
    try:
        server.worker_alive = lambda k: True
        okv3, err3 = server.verify_login_state(FakeStore(), ["douyin"])
        ok("5c 登录窗还开着 → 拒绝（会抢 profile）",
           (okv3 is False) and ("登录窗" in err3), (okv3, err3))
    finally:
        server.worker_alive = _orig_alive

    # 6 界面拿得到进度
    src = (APP / "server.py").read_text(encoding="utf-8")
    # ★ 又一例"文本扫描判据脆弱"（今天第三次）：原来钉的是 `"verify": dict(VERIFY_STATE)`
    #   这一整串，我给读加锁把变量名换成 _vsnap 之后它就假失败了。改成查**处理块**里有没有
    #   这两样东西 —— 只钉语义，不钉写法。
    _acc = src[src.find('if p == "/api/accounts":'):][:800]
    ok("6 /api/accounts 带 verify 进度", '"verify"' in _acc and "VERIFY_STATE" in _acc, _acc[:90])
    ui = (APP / "ui" / "app.js").read_text(encoding="utf-8")
    ok("6 界面有核验按钮与调用", "act-verify" in ui and "/api/accounts/verify" in ui)
    ok("6 index.html 有「核验登录态」按钮", "acVerifyAllBtn" in (APP / "ui" / "index.html").read_text(encoding="utf-8"))
    # ★ 路由放哪一段（2026-09-29 OCR 复查抓到 critical）：端点曾被错放进 do_GET，
    #   而界面用 POST 调 ⇒ 点了没反应，而"界面有调用 + 函数存在"两条文本判据都验不出来。
    ig, ip, ix = src.find("def do_GET"), src.find("def do_POST"), src.find('"/api/accounts/verify"')
    ok("6b 核验端点在 do_POST 段里（界面用 POST 调它）", 0 < ip < ix, (ig, ip, ix))

    # 7 没配 manage_url → 不猜（这条不用开浏览器：函数在开浏览器之前就返回）
    st, note = pw.verify_published("某标题", {"manage_url": ""}, "x", headless=True)
    ok("7 没配 manage_url → skip（不猜、不改状态）", (st, note) == ("skip", ""), (st, note))
    st2, note2 = pw.verify_published("", {"manage_url": "https://example.test/x"}, "x")
    ok("7 没标题 → skip", (st2, note2) == ("skip", ""), (st2, note2))

    # 8/9 接线
    pw_src = (APP / "publish_worker.py").read_text(encoding="utf-8")
    ok("8 SAU 成功分支调了 verify_published", "v_state, v_note = verify_published(" in pw_src)
    import json as _json
    _ads = _json.loads((APP / "publish_adapters.json").read_text(encoding="utf-8"))["platforms"]
    ok("9 抖音配了 manage_url（有证据）", "creator.douyin.com/creator-micro/content/manage" in
       str(_ads["douyin"].get("manage_url")), _ads["douyin"].get("manage_url"))
    empties = [k for k in ("channels", "kuaishou", "xhs", "weibo") if not _ads[k].get("manage_url")]
    ok("9 其余 SAU 平台留空（不猜 URL）", len(empties) == 4, empties)


    # 10 ★ 2026-09-29 真机：用户在收尾那一刻点「中止」→ 引擎记 canceled，但 worker 其实
    #    已把发布做完（状态文件里 success）⇒ 记录说谎 + 日配额少算（canceled 不计数）。
    import json as _j, tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        fp = Path(td) / "job.json"
        fp.write_text(_j.dumps({"state": "success", "note": "已发布 ✓"}), encoding="utf-8")
        ok("10 worker 已给 success → 返回 success（不许改写成 canceled）",
           server._worker_status_snapshot(fp)[0] == "success", server._worker_status_snapshot(fp)[0])
        fp.write_text(_j.dumps({"state": "running"}), encoding="utf-8")
        ok("10 worker 还在 running → 返回空（交给中止逻辑）",
           server._worker_status_snapshot(fp)[0] == "", server._worker_status_snapshot(fp)[0])
        fp.write_text("{坏 json", encoding="utf-8")
        ok("10 状态文件坏了 → 返回空（不误判）", server._worker_status_snapshot(fp)[0] == "")
        ok("10 文件不存在 → 返回空",
           server._worker_status_snapshot(Path(td) / "nope.json") == ("", ""))
        fp.write_text(_j.dumps({"state": "manual", "note": "等待验证码"}), encoding="utf-8")
        ok("10 note 与 state 同一次读出（不再各读一遍）", server._worker_status_snapshot(fp)[1] == "等待验证码", server._worker_status_snapshot(fp))


    # 11 ★ 真机（2026-09-29 job_53 B站，用户原话："点提交会让你选创作声明，还没选就一直卡在那里"）：
    #    创作声明是**必填下拉**（bcc-select，选项 li.bcc-option）—— 不选就点【立即投稿】毫无反应。
    #    要求：选中之后**回读输入框**自证；选不上就如实报警（不许默默往下走）。
    _ad_dec = {"declaration_trigger": "div.statement-content .bcc-select-input-wrap",
               "declaration_option": "含AI生成内容"}
    _fp = _FakeDecPage()
    _st = []
    _r = pw._select_declaration(_fp, _ad_dec, _st, "含AI生成内容")
    ok("11 选中后回读输入框自证", _r is True and any("创作声明已选" in x for x in _st), _st)
    _fp2 = _FakeDecPage(dead_option=True)          # 点得中但输入框不变 = 没选上
    _st2 = []
    _r2 = pw._select_declaration(_fp2, _ad_dec, _st2, "含AI生成内容")
    ok("11 没选上 → 如实报警、不谎报成功",
       _r2 is False and any("没选上" in x for x in _st2), _st2)
    _fp3 = _FakeDecPage()
    _st3 = []
    _r3 = pw._select_declaration(_fp3, {}, _st3, "含AI生成内容")   # 没配 trigger
    ok("11 没配 trigger → 直接跳过（对其它平台零影响）", _r3 is False and _st3 == [], _st3)
    _src2 = (APP / "publish_worker.py").read_text(encoding="utf-8")
    ok("11 提交前真的调了这一步", "if ad.get(\"declaration_option\"):" in _src2)
    _ads2 = _json.loads((APP / "publish_adapters.json").read_text(encoding="utf-8"))["platforms"]
    ok("11 B站配了创作声明（含AI生成内容）",
       _ads2["bilibili"].get("declaration_option") == "含AI生成内容",
       _ads2["bilibili"].get("declaration_option"))

    _fp4 = _FakeDecPage()
    _fp4.value = "含AI生成内容"                    # ★ 本来就已选中
    _st4 = []
    _r4 = pw._select_declaration(_fp4, _ad_dec, _st4, "含AI生成内容")
    ok("11 本来就已选中 → 也算成功（不误报失败）",
       _r4 is True and any("本来就已是" in x for x in _st4), _st4)
    _src3 = (APP / "publish_worker.py").read_text(encoding="utf-8")
    # ★ 判据必须**排除注释行**（今天第 4 次同类假失败）：我的修复注释里**引用了**那个错误的
    #   写法（`li.bcc-option:has(span:text-is("X"))`）用来解释为什么不能这么写 ——
    #   直接扫全文会把"解释用的反例"当成"代码里还在用"。
    _code_lines = [ln for ln in _src3.split(chr(10)) if not ln.strip().startswith("#")]
    _code = chr(10).join(_code_lines)
    ok("11 定位符不再把 Playwright 伪类塞进 :has()（OCR #1）",
       "bcc-option:has(" not in _code and ".filter(" in _code)
    ok("11 调用方用上了返回值（OCR #3）", "if not _select_declaration(page, ad, steps," in _src3)

    # ══════════ 12 核验的「完成」必须是真的完成（2026-09-30 实测踩过）══════════
    # 实测复现：把 profile 目录堵死（`browsers` 是个文件），8 个平台**全部**探测失败，
    # 而 probe_accounts 仍然 `return 0` ⇒ 服务端 ok=True ⇒ 记「核验完成，状态已按实写回」。
    # 这就是本项目最忌的「看起来成功」—— 而且它掩盖的正是**登录态本身**。
    _bad = APP / "tests" / "_tmp26_probe"
    shutil.rmtree(str(_bad), ignore_errors=True)
    (_bad / "data").mkdir(parents=True)
    (_bad / "data" / "browsers").write_text("not a dir", encoding="utf-8")  # 每个 profile 都建不出来
    _r = subprocess.run([sys.executable, "-X", "utf8", str(APP / "tools" / "probe_accounts.py"),
                         "--data-dir", str(_bad / "data"), "--verify"],
                        capture_output=True, timeout=300)
    _out = ((_r.stdout or b"") + (_r.stderr or b"")).decode("utf-8", "ignore")
    _lines = [ln for ln in _out.splitlines() if ln.strip()]
    ok("12 探测全失败 → 退出码非 0（不许假报成功）", _r.returncode != 0, _r.returncode)
    ok("12 最后一行是失败总结（服务端拿它当 note）",
       bool(_lines) and _lines[-1].startswith("核验失败：") and "平台探测失败" in _lines[-1],
       _lines[-1] if _lines else "(无输出)")
    ok("12 失败总结点名了平台（不只是数字）",
       "douyin" in (_lines[-1] if _lines else ""), _lines[-1] if _lines else "")

    # 13 服务端文案：成功**不许**拿「子进程最后一行」当结论 —— 那行是**某一个平台**的输出。
    #    实测证据：核验跑完后 /api/accounts 里 8 个平台的 note 挂的是同一条 xigua 的行。
    ok("13 成功文案固定为“核验完成”，不用子进程尾行",
       server.verify_done_note(True, "xigua     核验=waiting（等待扫码）账号=()") == "核验完成，状态已按实写回",
       server.verify_done_note(True, "xigua     核验=waiting（等待扫码）账号=()"))
    ok("13 失败文案用失败原因（点名平台）",
       server.verify_done_note(False, "核验失败：2 个平台探测失败：channels、kuaishou").startswith("核验失败："),
       server.verify_done_note(False, "x"))
    ok("13 失败又没拿到原因 → 兜底文案不撒谎", "失败" in server.verify_done_note(False, ""))

    # 14 「最后核验时间」要跨重启可读（DB 的 updated_at 只在状态/文案**变化**时才动，
    #    拿它当「最后核验时间」是错的 —— 今天核验过、结果没变，它还停在昨天）
    _sd = _bad / "store"
    (_sd / "browsers").mkdir(parents=True)
    (_sd / "browsers" / "douyin.status.json").write_text(json.dumps({
        "state": "on", "note": "已核验登录态：检测到已登录",
        "at": "2026-09-30 09:23:29", "verified_at": "2026-09-30 09:23:29"},
        ensure_ascii=False), encoding="utf-8")
    _store = server.Store(_sd)
    _store.refresh_accounts()
    _rows = {a["key"]: a for a in _store.list_accounts()}
    ok("14 账号行带 verified_at（最后核验时间）",
       _rows.get("douyin", {}).get("verified_at") == "2026-09-30 09:23:29",
       _rows.get("douyin", {}).get("verified_at"))
    ok("14 没核验过的平台 verified_at 为空（不编时间）",
       _rows.get("weibo", {}).get("verified_at") == "", _rows.get("weibo", {}).get("verified_at"))

    # 15 界面：核验失败要看得见（现在失败时界面一片安静，跟成功长得一样）
    _ui = (APP / "ui" / "app.js").read_text(encoding="utf-8")
    ok("15 界面显示核验失败", "vs.state === 'error'" in _ui and "核验失败" in _ui)
    ok("15 界面显示最后核验时间", "最后核验" in _ui and "verified_at" in _ui)

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

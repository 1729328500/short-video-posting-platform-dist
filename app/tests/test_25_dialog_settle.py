# -*- coding: utf-8 -*-
"""自测：弹窗收尾（dialog_settle）的判定层 —— 全部离线，不碰浏览器。

运行：cd app && python -X utf8 tests/test_25_dialog_settle.py

★ 背景（2026-09-29）：发布流程里"点一下弹出个层让你再确认一次"很常见，而各平台
  原先各写各的、普遍"点一次就当成功"——第一次「确定」后有二次确认时，后面的遮罩
  没人管，表现为很远的某一步「点不动」。本模块把它收敛成一条回路。
  设计：10-弹窗收尾-设计.md（§2.4 的"什么才算失败"是写计划时修正过的版本）。

判据（全是**离线**能验的逻辑）：
  1  没有可见层 → ok=True、rounds=0、**一次都没点**
  2  一层 +「确定」→ 点一次、clicked=["确定"]、ok=True
  3  **二次确认** → rounds=2、clicked=["确定","确定"]、ok=True
  4  层里只有「重选封面」→ **一次都没点**、skipped=1、ok=True（放过，不算失败）
  5  白名单命中多个 → 按 labels 顺序（「确定」先于「下一步」）
  6  点了但层数不降 → 换候选；候选走完仍不降 → stuck 非空、ok=False、**落盘被调用**
  7  轮次用尽且还剩"可收的层" → ok=False（不许当成功）
  8  ok=False 时必须调用 dump
  9  白名单里混进禁用词（labels 含「取消」）→ 仍然**绝不点**
  10 note 里含实际点到的文案（自证字段不为空）
  11 同层里同时有「取消」和「确定」→ 点「确定」（不能被禁用词带跑整层）
  12 文案带空白（" 确定 "）→ 仍能命中
  13 主表单层（无白名单按钮）+ 真弹层混在一起 → 挑到真弹层、收完 ok=True、不算误报
"""
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

import dialog_settle as ds  # noqa: E402

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


class FakeCtrl:
    """离线假 ctrl：dialogs = 每个可见层的按钮文案（按 DOM 顺序），每层带一个**身份 id**。

    为什么要有身份 id：二次确认的真实形态是"老层消失 + 新层**同一 tick** 出现"，
    层数 1→1 —— 只数个数区分不出"推进了"和"卡住了"（合成页面自测抓到的，见台账）。
    `swap={"确定": ["确定"]}` 就是模拟它：点中该文案时老层消失、同 tick 换个新层。
    """

    def __init__(self, dialogs, no_effect=(), unclickable=(), swap=None, dead_at=None):
        self.dialogs = [{"id": i, "b": list(b)} for i, b in enumerate(dialogs)]
        self._next = len(self.dialogs)
        self.swap = dict(swap or {})         # 点中该文案 → 老层消失 + 同 tick 新层出现
        self.no_effect = set(no_effect)      # 点了"不生效"的文案（层不关、内容不变）
        # ★ dead_at = {层id: (文案...)}：**只有那一层里的那个文案**点了没反应 ——
        #   用于复现真机形态：下层点不动、上层同名的那个才有效（2026-09-29 job 52 头条封面）
        self.dead = {k: set(v) for k, v in (dead_at or {}).items()}
        self.unclickable = set(unclickable)  # 点了返回 False 的文案
        self.mark = None                     # 被标记的那个层的 id
        self.clicks = []
        self.dumps = []

    def _get(self, h):
        for d in self.dialogs:
            if d["id"] == h:
                return d
        return None

    def visible_dialogs(self):
        return [d["id"] for d in self.dialogs]

    def dialog_count(self):
        return len(self.dialogs)

    def button_texts(self, dlg):
        d = self._get(dlg)
        return list(d["b"]) if d else []

    def click_button(self, dlg, text):
        if text in self.unclickable or text in self.dead.get(dlg, ()):
            return False
        self.clicks.append(text)
        if text in self.no_effect:
            return True
        d = self._get(dlg)
        if d is None:
            return False
        self.dialogs.remove(d)
        if text in self.swap:                # 老层没了，但**同一 tick** 冒出个新层（一次性：模拟"两步确认"）
            self.dialogs.append({"id": self._next, "b": list(self.swap.pop(text))})
            self._next += 1
        return True

    def set_mark(self, dlg):
        self.mark = dlg

    def wait_until_changed(self, before_count, before_texts, timeout):
        if self.dialog_count() < before_count:
            return True
        if self.mark is not None and self._get(self.mark) is None:
            return True
        if self.dialogs and list(self.dialogs[-1]["b"]) != list(before_texts):
            return True
        return False

    def url(self):
        return "https://example.test/page"

    def layers_info(self):
        """落盘用的层清单（设计 §2.4 要求带它）。"""
        return [{"sel": "fake", "idx": d["id"], "buttons": list(d["b"])} for d in self.dialogs]

    def dump(self, info):
        self.dumps.append(info)
        return "/tmp/fake-settle.json"


class _FakeHandle:
    """假 ElementHandle（供 set_mark 判断"那一层还在不在"）。"""

    def __init__(self, page, sel):
        self.page, self.sel = page, sel

    def is_visible(self):
        return self.page._count(self.sel) > 0

    def query_selector_all(self, sel):
        return []


class _FakeLoc:
    """够 set_cover / settle_sync 用的假 locator（同步 API 子集）。"""

    def __init__(self, page, sel, idx=0):
        self.page, self.sel, self.idx = page, sel, idx

    def count(self):
        return self.page._count(self.sel)

    # ★ 必须是**属性**不是方法：真 playwright 里 `locator.first` / `.last` 是属性。
    #   我第一版写成了方法 → `page.locator(sel).first` 拿到一个函数 → `.count()` 抛
    #   AttributeError → 被 set_cover 的宽 except 吞掉 → 假象是"无候选可见"。
    @property
    def first(self):
        return _FakeLoc(self.page, self.sel, 0)

    @property
    def last(self):
        n = self.page._count(self.sel)
        return _FakeLoc(self.page, self.sel, max(0, n - 1))

    def nth(self, i):
        return _FakeLoc(self.page, self.sel, i)

    def is_visible(self):
        return self.page._count(self.sel) > self.idx

    def click(self, timeout=None):
        self.page._on_click(self.sel)

    def scroll_into_view_if_needed(self, timeout=None):
        pass

    def set_input_files(self, path, timeout=None):
        self.page.uploaded.append(path)

    def inner_text(self, timeout=None):
        return self.page._text(self.sel)

    def locator(self, sel):
        return _FakeLoc(self.page, sel)

    def element_handle(self, timeout=None):
        return _FakeHandle(self.page, self.sel)

    def evaluate(self, js):
        return None


class _FakeCoverPage:
    """假 page：封面入口 + 图片文件框 + 一个待收的弹层，全都"找得到"。

    目的是让 `set_cover()` 走完完整路径（点入口 → 找文件框 → 投递 → 收尾），
    而不是像 20~28 那样只做文本扫描 —— 复查 finding 1 就是靠这种"真跑一遍"才抓到的。
    """

    def __init__(self):
        self.uploaded = []
        self.layer_open = False

    def _count(self, sel):
        if sel == "#t1":
            return 1                                  # 封面触发入口
        if "image" in sel or "jpg" in sel:
            return 1                                  # 图片文件框（_find_cover_file_input 会命中）
        if "blob:" in sel:
            return 1                                  # 封面预览（_wait_cover_preview 立刻返回）
        # 按钮：按子串匹配，别写死整串（BUTTON_SEL 会随真机证据调整 —— 2026-09-29
        # 因为 B站「完成」不是 <button> 而放宽过，写死会让这个假对象立刻失效）
        if "button" in sel or "[class*='btn']" in sel:
            return 1 if self.layer_open else 0
        if "dialog" in sel or "layer" in sel or "modal" in sel or "popup" in sel:
            return 1 if self.layer_open else 0        # 弹层：点了入口才出现
        return 0

    def _text(self, sel):
        return "确定" if ("button" in sel or "[class*='btn']" in sel) else ""

    def _on_click(self, sel):
        if sel == "#t1":
            self.layer_open = True                    # 点入口 → 弹层出现
        elif "button" in sel or "[class*='btn']" in sel:
            self.layer_open = False                   # 点「确定」→ 弹层关掉

    def locator(self, sel):
        return _FakeLoc(self, sel)


def main():
    # 1 没有可见层
    c = FakeCtrl([])
    r = ds.settle_with(c)
    ok("1 无层 → ok=True", r["ok"] is True, r)
    ok("1 无层 → rounds=0 且一次没点", r["rounds"] == 0 and c.clicks == [], c.clicks)

    # 2 一层 + 确定
    c = FakeCtrl([["确定"]])
    r = ds.settle_with(c)
    ok("2 一层确定 → 点了 1 次", c.clicks == ["确定"], c.clicks)
    ok("2 一层确定 → ok=True、clicked 正确",
       r["ok"] and r["clicked"] == ["确定"] and r["rounds"] == 1, r)

    # 3 二次确认
    c = FakeCtrl([["确定"], ["确定"]])
    r = ds.settle_with(c)
    ok("3 二次确认 → rounds=2 / clicked 两条",
       r["rounds"] == 2 and r["clicked"] == ["确定", "确定"] and r["ok"], r)

    # 4 只有禁用按钮 → 放过
    c = FakeCtrl([["重选封面"]])
    r = ds.settle_with(c)
    ok("4 只有禁用按钮 → 一次没点", c.clicks == [], c.clicks)
    ok("4 只有禁用按钮 → ok=True 且 skipped=1",
       r["ok"] and r["skipped"] == 1 and r["stuck"] == [], r)

    # 5 顺序按 labels
    c = FakeCtrl([["下一步", "确定"]])
    r = ds.settle_with(c)
    ok("5 多个命中 → 取 labels 里更靠前的「确定」", r["clicked"] == ["确定"], r)

    # 6 点了不降 → 换候选 → stuck
    c = FakeCtrl([["确定"]], no_effect=["确定"])
    r = ds.settle_with(c)
    ok("6 点了不降 → ok=False 且 stuck 非空", (not r["ok"]) and r["stuck"], r)
    ok("6 点了不降 → 落盘被调用", len(c.dumps) == 1, c.dumps)

    # 7 轮次用尽
    c = FakeCtrl([["确定"]] * 5)
    r = ds.settle_with(c, budget=4)
    ok("7 轮次用尽还有层 → ok=False", (not r["ok"]) and r["rounds"] == 4, r)

    # 8 ok=False 必落盘（与 6 重复但单独留一条语义判据）
    c = FakeCtrl([["完成"]], no_effect=["完成"])
    r = ds.settle_with(c)
    ok("8 ok=False → dump 被调用且路径回填",
       len(c.dumps) == 1 and r["dump"] == "/tmp/fake-settle.json", r)

    # 9 禁用优先于白名单
    c = FakeCtrl([["取消"]])
    r = ds.settle_with(c, labels=("取消", "确定"))
    ok("9 白名单混进「取消」→ 绝不点", c.clicks == [], c.clicks)

    # 10 note 自证
    c = FakeCtrl([["确定"], ["确定"]])
    r = ds.settle_with(c)
    ok("10 note 含实际点到的文案",
       "确定" in r["note"] and "2 轮" in r["note"], r["note"])

    # 11 同层「取消」+「确定」
    c = FakeCtrl([["取消", "确定"]])
    r = ds.settle_with(c)
    ok("11 同层有取消和确定 → 点确定", c.clicks == ["确定"], c.clicks)

    # 12 文案带空白
    c = FakeCtrl([[" 确定 "]])
    r = ds.settle_with(c)
    ok("12 文案带空白 → 仍能命中", r["clicked"] == ["确定"], r)

    # 13 主表单层 + 真弹层
    c = FakeCtrl([["发布", "上传封面"], ["确定"]])
    r = ds.settle_with(c)
    ok("13 混着主表单层 → 挑到真弹层并点掉", c.clicks == ["确定"] and r["ok"], (c.clicks, r))
    ok("13 主表单层 → 不算误报（skipped 记到，stuck 为空）",
       r["stuck"] == [] and r["skipped"] == 1, r)

    # 19 ★ 二次确认的**真实形态**：老层消失、新层同一 tick 出现（层数 1→1）。
    #    合成页面自测抓到的：老判据"可见层数变少"观察不到那个瞬时 0，会把"推进了"误判成"收不动"。
    c = FakeCtrl([["确定"]], swap={"确定": ["确定"]})
    r = ds.settle_with(c)
    ok("19 老层消失+新层同 tick 出现 → 仍算推进（rounds=2、ok=True）",
       r["rounds"] == 2 and r["ok"] and r["clicked"] == ["确定", "确定"], r)

    # 35 ★ 真机（2026-09-29 job 52 头条封面，截图里看到嵌套确认框）：点「完成裁剪」后又弹出
    #    一个确认框，两层里**都有「确定」**。老实现只盯一个层 + 按**文案**去重 ⇒
    #    在"先试的那层"点「确定」点不动之后，就再也不会去试另一层里同名的「确定」⇒ 报"收不动"。
    #    这里让 **id1 的确定点不动、id0 的确定有效**（reversed 会先试 id1）。
    #    ⚠️ 期望要写准：id1 那层**永远点不动**，所以最终仍应如实报 stuck（那是诚实的）；
    #    本条要验的是**"有没有继续去试 id0 并真的推进"** —— 旧逻辑在这条会 `clicked=[]`、rounds=0
    #    （只试了 id1 那个点不动的确定就收工），新逻辑会点中 id0 的确定（rounds≥1）。
    c = FakeCtrl([["确定", "取消"], ["确定"]], dead_at={1: ["确定"]})
    r = ds.settle_with(c)
    ok("35 一层点不动时，会继续试另一层里同名的按钮（并真的推进）",
       r["clicked"] == ["确定"] and r["rounds"] >= 1, r)

    # 14 落盘：JSON 真的写出来了、内容含 note（假 page 的截图会抛，正好验证"截图失败不影响 JSON"）
    import tempfile, json as _json
    class _FakePage:
        def screenshot(self, **kw):
            raise RuntimeError("假 page 不会截图")
    with tempfile.TemporaryDirectory() as td:
        p = ds.dump_settle_failure(_FakePage(), {"note": "收不动", "clicked": ["确定"]}, td)
        ok("14 落盘 → 返回 JSON 路径且文件存在", bool(p) and Path(p).exists(), p)
        if p:
            data = _json.loads(Path(p).read_text(encoding="utf-8"))
            ok("14 落盘 → JSON 里有 note", data.get("note") == "收不动", data)
    ok("14 dump_dir=None → 不落盘也不报错",
       ds.dump_settle_failure(_FakePage(), {"note": "x"}, None) is None)

    # 15 两者共用同一套常量（防两条回路各走各的）
    import inspect as _ins
    src = Path(APP / "dialog_settle.py").read_text(encoding="utf-8")
    ok("15 模块里存在 settle_sync", "def settle_sync(" in src)
    ok("15 模块里存在 settle_async", "async def settle_async(" in src)
    ok("15 模块里存在遮挡检查", "def is_occluded_by_layer(" in src)

    # 16 ★ 两条回路必须同步演进：异步那条也要用 pick_label 和同一套常量
    a_src = src[src.find("async def settle_async("):]
    a_src = a_src[:a_src.find("\ndef ", 10)] if "\ndef " in a_src[10:] else a_src
    ok("16 异步回路也调 pick_label", "pick_label(" in a_src)
    ok("16 异步回路也用 DEFAULT_LABELS/DEFAULT_DENY",
       "DEFAULT_LABELS" in a_src and "DEFAULT_DENY" in a_src)
    ok("16 异步回路也认 budget", "budget" in a_src)
    # ★ 2026-09-29 升级：上面几条只钉"词在不在"，检测不出**语义漂移**，更检测不出
    #   "修一份漏一份"。补一条钉住**两份共有的修复痕迹**：strip 去重必须各出现一次，
    #   总数正好 2（同步 + 异步）。改一份忘一份 → 这条立刻红。
    ok("16 两份回路都用了 strip 去重（防修一份漏一份）",
       src.count("t.strip() not in tried") == 2, src.count("t.strip() not in tried"))
    ok("16 两份回落都带了'目标自己'的按钮比对（query_selector_all 各一次）",
       src.count("query_selector_all") == 2, src.count("query_selector_all"))

    # 17 ★ 遮挡检查：hints 里的 :visible 进浏览器前必须去掉（浏览器不认这个伪类）
    ok("17 JS 里没把 :visible 直接塞给 elementFromPoint 的 matches",
       "':visible'" not in src[src.find("_JS_OCCLUDED"):src.find("def is_occluded_by_layer")])
    ok("17 有去 :visible 的动作", 'replace(":visible"' in src)

    # 20~24 适配器路径（publish_worker）的接线 —— 这条链没法离线跑真机，只能钉住"接线在不在"
    pw = (APP / "publish_worker.py").read_text(encoding="utf-8")
    ok("20 publish_worker 导入了 dialog_settle", "import dialog_settle" in pw)
    ok("20 DIALOG_HINTS 改成引用模块（单一事实来源）",
       "DIALOG_HINTS = dialog_settle.DEFAULT_HINTS" in pw)
    ok("21 老的 _dismiss_cover_dialog 已删除（避免两套收尾打架）",
       "_dismiss_cover_dialog" not in pw)
    ok("22 封面后调了统一收尾", "settle_sync(" in pw)
    # ★ 2026-09-29 改判据：原来钉的是字串 `不点发布`，而独立复查 finding 2 之后
    #   文案改成走 `_blocked_submit_status()`（"未点提交"），字串一变判据就假失败。
    #   改成钉**机制**：有遮挡检查、有状态文案助手、有 submit 分支；文案语义由判据 30 覆盖。
    ok("23 点发布前有遮挡硬闸",
       "is_occluded_by_layer(" in pw and "_blocked_submit_status(" in pw
       and "if args.submit:" in pw)
    ok("24 封面相关的固定 sleep 已改成条件等待（wait_after_cover_trigger 不再裸 sleep）",
       'time.sleep(ad.get("wait_after_cover_trigger"' not in pw)

    # 25~28 SAU 路径：微博那处接线 + NOTICE 记账
    wb = (APP / "vendor" / "sau" / "uploader" / "weibo_uploader" / "main.py").read_text(encoding="utf-8")
    ok("25 微博上传器导入了 settle_async（带兜底）",
       "from dialog_settle import settle_async" in wb and "settle_async = None" in wb)
    ok("26 微博在封面之后调了收尾", "_upload_thumbnail" in wb and "封面后弹窗收尾" in wb)
    ok("27 调用点在封面层关闭之后（不早于「封面已上传并完成」）",
       wb.find("封面已上传并完成") < wb.find("封面后弹窗收尾"))
    notice = (APP / "vendor" / "sau" / "NOTICE.md").read_text(encoding="utf-8")
    ok("28 NOTICE 记到第 12 处", "共 12 处" in notice and "除上表 12 处外" in notice)

    # ===== 独立复查（2026-09-29）抓到的缺陷的回归判据 =====
    # 29 ★ 复查 finding 1（Critical）：`set_cover()` 里引用了**不存在的 args**
    #    → NameError 被那个宽 except 吞掉 → 伪装成"没找到封面入口（文案可能又变了）"，
    #      而封面其实**已经投递成功**却被 Escape 丢掉。这是"把代码 bug 报成配置问题"，
    #      正是项目最恨的形态；而 20~28 全是文本扫描，所以 40 条全绿也没抓住它。
    import publish_worker as _pw
    _steps = []
    _okc = _pw.set_cover(_FakeCoverPage(), {"cover_trigger": "#t1"}, "/tmp/fake-cover.png", _steps)
    ok("29 set_cover 真的走到投递完成（不是把代码 bug 报成找不到入口）",
       _okc is True and any("封面已投递" in s for s in _steps), _steps)
    ok("29 封面后调了统一收尾（收尾行出现）",
       any("封面后弹层收尾" in s for s in _steps), _steps)
    ok("29 没有把异常报成配置问题", not any("需要重新标定" in s for s in _steps), _steps)

    # 31 ★ 复查 finding 5：文案带空白时，候选去重必须按 **strip 后**比 ——
    #    否则 `" 确定 " not in ["确定"]` 恒真，同一个按钮被点满 budget 次，
    #    而且"卡住原因"里会把同一候选重复列一串。
    c = FakeCtrl([[" 确定 "]], no_effect=["确定"])
    r = ds.settle_with(c)
    ok("31 带空白的按钮只点一次（不是点满预算）", c.clicks == ["确定"], c.clicks)
    ok("31 卡住原因里不重复同一候选",
       bool(r["stuck"]) and "确定、确定" not in r["stuck"][0], r["stuck"])

    # 30 ★ 复查 finding 2：挡住提交按钮时，文案必须进 **note**（UI 只显示 note/error，
    #    不渲染 steps），否则用户在停手模式下看不到任何警告、照着提示去点一个遮罩。
    _st_f, _note_f = _pw._blocked_submit_status("头条号", "被 div.x 挡住", True)
    _st_m, _note_m = _pw._blocked_submit_status("头条号", "被 div.x 挡住", False)
    ok("30 自动提交模式 → 判失败、文案说未点提交",
       _st_f == "fail" and "未点提交" in _note_f and "div.x" in _note_f, (_st_f, _note_f))
    ok("30 停手模式 → manual、文案含挡住原因（用户看得到）",
       _st_m == "manual" and "div.x" in _note_m and "弹层" in _note_m, (_st_m, _note_m))

    # ===== 真机批次 14（2026-09-29）两条失败驱动的判据 =====
    # 32 ★ job 48（B站）：滚动那步用 get_by_role 找，硬闸用 submit_button 找 —— 两处认的不是
    #    同一个元素（B站按钮是 <span class="submit-add">，不是 <button>）⇒ 没滚到视野
    #    ⇒ 硬闸 elementFromPoint 取不到 ⇒ 整条失败。判据：两处**共用**同一个解析函数。
    # ★ 计数要带 `= ` 前缀：只算**调用点**，别把 `def _resolve_submit_button(page, ad):` 也算进去
    #   （我第一版就踩了这个 —— 文本扫描判据最爱犯的错）
    _n_resolve = pw.count("= _resolve_submit_button(page, ad)")
    ok("32 滚动与硬闸共用同一个提交按钮解析（各一次）", _n_resolve == 2, _n_resolve)

    # 33 ★ job 47（头条）：封面弹窗里真实按钮是「**完成裁剪**」，精确匹配下 完成裁剪≠完成
    #    ⇒ 去点了层里那个点不动的「确定」⇒ 收不动 ⇒ 弹窗一路挡到发布。
    #    判据：adapter 的 modal_labels 真的传进了收尾调用。
    _n_labels = pw.count('.get("modal_labels")')
    ok("33 modal_labels 传进了收尾（封面那处 + 三个步骤之前）", _n_labels >= 2, _n_labels)
    _ads = _json.loads((APP / "publish_adapters.json").read_text(encoding="utf-8"))["platforms"]
    ok("33 头条/西瓜都配了「完成裁剪」",
       all("完成裁剪" in _json.dumps(_ads.get(k) or {}, ensure_ascii=False)
           for k in ("toutiao", "xigua")),
       [_ads.get(k, {}).get("modal_labels") for k in ("toutiao", "xigua")])

    # 34 ★ 行为级（不是文本扫描）：_resolve_submit_button 三态
    class _RoleFake:
        def __init__(self, n):
            self.n = n

        def count(self):
            return self.n

        @property
        def first(self):
            return "F"

        @property
        def last(self):
            return "L"

    _fp = _FakeCoverPage()
    _fp.get_by_role = lambda role, name=None, exact=False: _RoleFake(0)
    _l1, _h1 = _pw._resolve_submit_button(_fp, {"submit_button": "#t1", "submit_text": "发布"})
    ok("34 优先用 submit_button 选择器", _l1 is not None and "#t1" in str(_h1), _h1)
    _fp2 = _FakeCoverPage()
    _fp2.get_by_role = lambda role, name=None, exact=False: _RoleFake(1)
    _l2, _h2 = _pw._resolve_submit_button(_fp2, {"submit_text": "发布"})
    ok("34 没配 submit_button 时退回 role", _l2 is not None and "role" in str(_h2), _h2)
    _fp3 = _FakeCoverPage()
    _fp3.get_by_role = lambda role, name=None, exact=False: _RoleFake(0)
    _l3, _h3 = _pw._resolve_submit_button(_fp3, {"submit_text": "发布"})
    ok("34 两路都找不到 → 如实返回 None", _l3 is None, _h3)

    passed = sum(1 for _, cd in RESULTS if cd)
    total = len(RESULTS)
    print("\n===== 结果: %d/%d 通过 =====" % (passed, total))
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

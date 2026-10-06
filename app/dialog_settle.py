# -*- coding: utf-8 -*-
"""弹窗收尾（settle）：把页面上"弹层"逐个收掉，直到没有收不动的为止。

设计：`10-弹窗收尾-设计.md`（2026-09-29）。

★ 为什么要有它
  发布流程里到处是"点一下弹个层让你再确认一次"。各平台原先各写各的，而且普遍
  "点一次就当成功"——第一次「确定」之后若还有二次确认，后面的遮罩没人管，
  表现为很远的某一步「点不动」（设计 §1.1 列了三个缺陷，含 09-28 头条的实测）。

★ 两条硬规矩（本项目 09-28 纪律：每条"成功"都要有一条能自证的话）
  ① 返回值只有两种：ok=True（没有收不动的层）或 ok=False（有）。**没有"我试过了"。**
  ② 禁用文案是硬闸：宁可不动，也不点不确定的按钮（发布/提交/取消/重选/删除…）。

★ 两种"层"要分清（写计划时发现并修正过一版，别再踩）
  · 有白名单按钮、点了数不降            → 真卡住 → ok=False（落盘）
  · **一个白名单按钮都没有**的层        → 我们本来就无能为力 → 放过、记 skipped，
    **不算失败**。微博的**主表单本身就是** `div.wbpro-layer`，会被结构识别匹配到；
    若把它当失败，就成了天天误报的"狼来了"，失败信号反而没人信。

★ ctl 协议（本模块只调这几个方法，同步/异步各实现一份）
    visible_dialogs() -> list        # 可见层句柄；⚠️ **顺序是"按 hint 选择器分组"，不是 DOM 顺序**
                                     #   （2026-09-29 OCR 复查指出：这句原来写成"按 DOM 顺序
                                     #   （最后一个＝最上面）"，与实现不符 —— 任何"取最后一个当最上面"
                                     #   的用法都是错的，第 3 个信号就踩过这个坑）
    dialog_count() -> int
    button_texts(dlg) -> list[str]   # 该层内按钮文案（inner_text 去空白）
    click_button(dlg, text) -> bool  # 按**精确文案**点；点不到返回 False
    set_mark(dlg) -> bool            # 给"要点的这一层"打标记（用于判断它真的没了）
    wait_until_changed(before_count, before_texts, timeout) -> bool
                                     # 条件等待：这一步**真的推进了**（三信号，见下）
    url() -> str
    dump(info) -> str | None

★ 为什么"推进了"要三个信号（2026-09-29 合成页面自测抓到的）
  初版只看「可见层数变少」。但二次确认的真实形态是**老层消失 + 新层同一 tick 出现** ——
  层数 1→1，中间那个瞬时 0 **观察不到**，于是明明推进了却被判成"收不动"。
  ⇒ 三信号任一命中即算推进：
     ① 可见层数变少（保留老判据，覆盖"层被关掉"）
     ② **打了标记的那层没了**（元素身份，覆盖"同 tick 换层"）
     ③ 最上面那层的按钮文案变了（覆盖"原地换步"，比如一步表单进到下一步）
  ③ 单独命中时也可能只是"别处冒了个新层" ⇒ 只多花轮次，**不会误报成功**：
  下一轮重扫还会看到原来那层，继续点或最终判 stuck。
"""
import asyncio
import json
import os
import time
from pathlib import Path

DEFAULT_HINTS = (
    "[class*='dialog']:visible", "[class*='layer']:visible",
    "[class*='modal']:visible", "[class*='popup']:visible", "[role='dialog']:visible",
)
DEFAULT_LABELS = ("确定", "完成", "保存", "使用", "确认", "我知道了", "下一步", "继续", "同意")
DEFAULT_DENY = ("发布", "提交", "取消", "重选", "重新上传", "删除")
DEFAULT_BUDGET = 4
DEFAULT_SETTLE_TIMEOUT = 3.0

# 层内"按钮"的候选选择器。
# ★ 2026-09-29 **真机证据**（B站 job41 的 hold.html + settle 截图）：
#   B站封面弹窗的「完成」**不是 `<button>`** —— 整页 HTML 里 `<button ...>完成` **0 命中**，
#   它是 `<div class="...btn">完成</div>` 这类。只认 `button, [role=button]` 的后果是：
#   真按钮读不到，反倒从**隐藏的**嵌套确认框里读到一个「确定」（白名单里排第一）
#   → 去点一个点不到的东西 → `rounds:0` + 报"收不动"（诚实但没用）。
#   ⇒ 选择器放宽到常见的几类可点元素；安全性仍靠"**精确文案**白名单 + 禁用名单"兜着。
BUTTON_SELS = ("button", "[role=button]", "a", "[class*='btn']", "[class*='button']")
# ★ 每个选择器都要**自己的** `:visible` —— 写成 "button, [role=button]:visible" 时，
#   伪类只作用于列表里最后一个。而且可见性过滤是必须的：隐藏的那个「确定」就是这么
#   被误选中的（真机证据同上）。
BUTTON_SEL = ", ".join(s + ":visible" for s in BUTTON_SELS)


def is_denied(text, deny=DEFAULT_DENY):
    """文案是否命中禁用词（**子串**命中：「取消确定」也算禁用）。

    ★ 为什么用子串：禁用名单是**安全闸**，宁可错杀 —— 多点一次无害，点到
      「重选封面」/「取消」就出事（09-28 头条实测：「重选封面」是 .btn-cancel）。
    """
    t = str(text or "").strip()
    return any(d in t for d in (deny or DEFAULT_DENY))


def pick_label(texts, labels=DEFAULT_LABELS, deny=DEFAULT_DENY):
    """从层内按钮文案里挑出该点的那个；挑不出返回 None。

    规则（顺序即优先级）：
      ① 先把命中禁用词的候选剔掉（**禁用优先于白名单**：labels 里混进「取消」也不许点）；
      ② 再按 labels 的顺序取第一个**精确相等**的（不是子串 —— 见项目「坑 6」）。
    """
    safe = []
    for t in texts or []:
        t = str(t or "").strip()
        if not t or is_denied(t, deny):
            continue
        safe.append(t)
    for lb in labels or ():
        if is_denied(lb, deny):
            continue
        if lb in safe:
            return lb
    return None


def settle_with(ctrl, *, labels=None, deny=None,
                budget=DEFAULT_BUDGET, settle_timeout=DEFAULT_SETTLE_TIMEOUT):
    """核心回路（与浏览器无关）。返回见模块 docstring。"""
    labels = tuple(labels or DEFAULT_LABELS)
    deny = tuple(deny or DEFAULT_DENY)
    budget = max(1, int(budget))
    clicked, skipped, rounds = [], 0, 0
    reason = ""                      # 非空 ⇒ 没收拾干净

    while rounds < budget:
        dlgs = ctrl.visible_dialogs()
        if not dlgs:
            break
        # ★ 候选层 = **所有**"有白名单按钮"的层，从最上面往下（2026-09-29 真机：头条封面弹窗
        #   点「完成裁剪」后又弹出一个**嵌套确认框**，两层里都有「确定」—— 老实现只盯一个层
        #   且按**文案**去重 ⇒ 在下面那层点「确定」被嵌套框的遮罩挡住之后，
        #   就再也不会去试上面那层里同名的「确定」了，于是报"收不动"（截图里看得清楚）。
        cands = [d for d in reversed(dlgs)
                 if pick_label(ctrl.button_texts(d), labels, deny) is not None]
        if not cands:
            skipped = len(dlgs)      # 全是"无可点按钮"的层 → 放过，不算失败
            break

        progressed, seen = False, []
        for target in cands:         # ★ 逐层试；哪一层先推进就用哪一层
            if progressed or rounds >= budget:
                break
            texts = ctrl.button_texts(target)
            tried = []               # ★ 每层各自记"试过什么"（不再跨层按文案去重）
            while rounds < budget:
                lb = pick_label([t for t in texts if t.strip() not in tried], labels, deny)
                if lb is None:
                    break
                before = ctrl.dialog_count()
                ctrl.set_mark(target)
                if not ctrl.click_button(target, lb):
                    tried.append(lb)
                    seen.append(lb)
                    continue
                rounds += 1
                if ctrl.wait_until_changed(before, texts, settle_timeout):
                    clicked.append(lb)
                    progressed = True
                    break
                tried.append(lb)
                seen.append(lb)
        if not progressed:
            reason = "所有可点的层都试过了、都没推进（试过：%s）" % ("、".join(seen) or "无白名单命中")
            break

    if not reason and rounds >= budget:
        # 轮次用尽：还要再看一眼剩下的层"收不收得动" —— 收得动才算没干完
        dlgs = ctrl.visible_dialogs()
        todo = [d for d in dlgs if pick_label(ctrl.button_texts(d), labels, deny) is not None]
        if todo:
            reason = "轮次用尽（budget=%d，还剩 %d 个可收的层）" % (budget, len(todo))
        else:
            skipped = len(dlgs)

    remaining = ctrl.dialog_count()
    if reason:
        stuck = [reason]
        note = "弹层收不动：%s（已点：%s；可见层 %d）" % (reason, "、".join(clicked) or "无", remaining)
        # ★ 落盘必须带**可见层清单**（设计 §2.4 要求）：
        #   2026-09-29 真机教训 —— B站那次只写了结论（"层内按钮：确定/取消"），
        #   要标定还得回头翻 hold.html。现在把每层的按钮文案一起落下。
        try:
            _layers = ctrl.layers_info()
        except Exception:                                # noqa: BLE001
            _layers = []
        path = ctrl.dump({"note": note, "rounds": rounds, "clicked": clicked,
                          "stuck": stuck, "remaining": remaining, "skipped": skipped,
                          "url": ctrl.url(), "layers": _layers})
        return {"ok": False, "rounds": rounds, "clicked": clicked, "stuck": stuck,
                "remaining": remaining, "skipped": skipped, "note": note, "dump": path}

    note = "%s（%d 轮，弹层已清空%s）" % (
        " → ".join(clicked) or "本来就没有可收的弹层", rounds,
        "" if not skipped else "；放过 %d 个无可点按钮的层" % skipped)
    return {"ok": True, "rounds": rounds, "clicked": clicked, "stuck": [],
            "remaining": remaining, "skipped": skipped, "note": note, "dump": None}


# ---------------- 落盘（"失败必须给证据"） ----------------

def _write_json_atomic(path, obj):
    """原子写（tmp + os.replace）。

    ★ 照 09-28 的 OCR 发现办的：非原子写会撞上"读了一半"，把一次瞬时读错误
      变成落库的错状态。写到别的进程要读的文件（比如给别人标定用）尤其要。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # ★ tmp 名带 pid（2026-09-29 OCR 复查指出）：固定名在并发写下会互相踩 ——
    #   虽然当前引擎一次只跑一个发布进程，但"靠调用方保证不并发"不是原子写的保证。
    tmp = path.with_name(path.name + ".%d.tmp" % os.getpid())
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def dump_settle_failure(page, info, dump_dir):
    """把"收不动"的现场落盘，供标定用；返回 JSON 路径（写不了返回 None）。

    ★ 与 publish_worker 的 `job_*.hold.json`、微博的 `weibo_decl_failure.json`
      同属决策 3.1/3.4「失败必须给证据，不能只是说明」。
    ★ 截图失败**不影响** JSON 落盘（分开 try）—— 证据里最值钱的是文案与结构。
    """
    if not dump_dir:
        return None
    path = Path(dump_dir) / "settle_failure.json"
    try:
        _write_json_atomic(path, info)
    except Exception:            # noqa: BLE001
        return None
    try:
        page.screenshot(path=str(Path(dump_dir) / "settle_failure.png"))
    except Exception:            # noqa: BLE001
        pass
    return str(path)


async def dump_settle_failure_async(page, info, dump_dir):
    """同上，但用 async page 的截图。"""
    if not dump_dir:
        return None
    path = Path(dump_dir) / "settle_failure.json"
    try:
        _write_json_atomic(path, info)
    except Exception:            # noqa: BLE001
        return None
    try:
        await page.screenshot(path=str(Path(dump_dir) / "settle_failure.png"))
    except Exception:            # noqa: BLE001
        pass
    return str(path)


# ---------------- 同步绑定（适配器路径 publish_worker 用） ----------------

class SyncPlaywrightCtrl:
    """把同步 playwright 的 page 包成 `settle_with` 要的 ctrl。

    ★ 手写的 `inner_text(...) == text` 精确比对，而**不是** `:has-text` 子串 ——
      本项目的「坑 6」：微博 `has_text="视频"` 点到了首页的「视频音乐」。
      而且不用 `:text-is` 是为了让"取文案"和"点按钮"用同一套判断，避免
      inner_text 与 :text-is 在规范化上的细微差别导致"看得到却点不到"。
    """
    PER_SEL_CAP = 4          # 同一种选择器最多算 4 个，够用且防爆
    PER_BTN_CAP = 24         # 放宽选择器后层里匹配会变多，上限跟着抬（见 BUTTON_SELS 的说明）

    def __init__(self, page, *, hints=None, dump_dir=None):
        self.page = page
        self.hints = tuple(hints or DEFAULT_HINTS)
        self.dump_dir = dump_dir
        self._handle = None          # 被标记那层的元素句柄
        self._marked_ok = False      # 句柄是否真的拿到了（拿不到就不信"身份"这条信号）

    def visible_dialogs(self):
        out = []
        for sel in self.hints:
            try:
                n = self.page.locator(sel).count()      # hints 自带 :visible
            except Exception:                            # noqa: BLE001
                continue
            for i in range(min(n, self.PER_SEL_CAP)):
                out.append((sel, i))
        return out

    def dialog_count(self):
        return len(self.visible_dialogs())

    def _root(self, dlg):
        sel, i = dlg
        return self.page.locator(sel).nth(i)

    def button_texts(self, dlg):
        try:
            loc = self._root(dlg).locator(BUTTON_SEL)
            n = loc.count()
            out = []
            for j in range(min(n, self.PER_BTN_CAP)):
                try:
                    out.append((loc.nth(j).inner_text(timeout=2000) or "").strip())
                except Exception:                        # noqa: BLE001
                    out.append("")
            return out
        except Exception:                                # noqa: BLE001
            return []

    def click_button(self, dlg, text):
        try:
            loc = self._root(dlg).locator(BUTTON_SEL)
            n = loc.count()
            for j in range(min(n, self.PER_BTN_CAP)):
                try:
                    if (loc.nth(j).inner_text(timeout=2000) or "").strip() == text:
                        loc.nth(j).click(timeout=5000)
                        return True
                except Exception:                        # noqa: BLE001
                    continue
        except Exception:                                # noqa: BLE001
            return False
        return False

    # ★ 给"要点的那个层"记一个**元素句柄**，用于判断"它是不是真没了"。
    #   2026-09-29 独立复查指出：早先"往 DOM 上打 data-settle-mark 属性"的写法有两个洞 ——
    #     ① 属性只在下次 set_mark 时才清 → 收尾结束后页面上残留一个标记，
    #        会被 hold.html 那类**整页 HTML 落盘**带进标定材料；
    #     ② 用 `[data-settle-mark]` 计数**不看可见性** → 平台用 display:none 关层
    #        （微博就是这么关的：主表单层从 none 变回可见）时，这条信号永远不触发。
    #   元素句柄把两条一起解决：节点被移除**或**被隐藏，`is_visible()` 都是 False，
    #   而且不在页面里留任何痕迹。
    def set_mark(self, dlg):
        try:
            self._handle = self._root(dlg).element_handle()
            self._marked_ok = self._handle is not None
        except Exception:                                # noqa: BLE001
            self._handle = None
            self._marked_ok = False                      # 记不上就别信"身份"这条信号
        return self._marked_ok

    def wait_until_changed(self, before_count, before_texts, timeout):
        """等"这一步真的推进了"—— 三信号任一命中即算（模块 docstring 有说明）。

        ★ 条件等待，不是 sleep 赌时间；读不出来时**算推进**（不卡在这，
          后面还会再扫一遍，最终由 budget/stuck 兜底）。
        ★ 第三信号比的是**目标自己**的按钮文案（before_texts 就是目标的）。
          复查指出：早先比的是 `visible_dialogs()[-1]`，而那个列表是**按 hint 分组**的、
          不是 DOM 顺序 —— 比的可能是另一个层，于是恒不相等、白白算成"推进"，
          把一个真卡住的情形报成"轮次用尽"（多点了好几次真按钮）。
        """
        end = time.monotonic() + max(0.2, float(timeout))
        while True:
            try:
                if self.dialog_count() < before_count:
                    return True
                if self._marked_ok:
                    if not self._handle.is_visible():
                        return True                      # ② 那一层没了 / 被隐藏
                    now = []
                    for el in (self._handle.query_selector_all(BUTTON_SEL) or []):
                        try:
                            now.append((el.inner_text() or "").strip())
                        except Exception:                # noqa: BLE001
                            now.append("")
                    if [t for t in now if t] != [t for t in before_texts if t]:
                        return True                      # ③ **目标自己**的按钮变了
            except Exception:                            # noqa: BLE001
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.2)

    def url(self):
        try:
            return self.page.url
        except Exception:                                # noqa: BLE001
            return ""

    def layers_info(self):
        """可见层清单（选择器 + 每层前若干按钮文案）—— 落盘用，供标定。

        ★ 为什么要有（2026-09-29 真机教训）：B站那次落盘只写了结论
          （"层内按钮：确定/取消"），标定时还得回头翻 hold.html 才敢判断
          "是点错了层、还是那个按钮根本不是 <button>"。设计 §2.4 本来就要求带层清单。
        """
        out = []
        try:
            for sel, idx in self.visible_dialogs():
                out.append({"sel": sel, "idx": idx,
                            "buttons": self.button_texts((sel, idx))[:12]})
        except Exception:                                # noqa: BLE001
            pass
        return out

    def dump(self, info):
        return dump_settle_failure(self.page, info, self.dump_dir)


def settle_sync(page, *, hints=None, labels=None, deny=None, budget=DEFAULT_BUDGET,
                settle_timeout=DEFAULT_SETTLE_TIMEOUT, dump_dir=None):
    """同步入口（适配器路径用）。参数与返回见模块 docstring。"""
    ctrl = SyncPlaywrightCtrl(page, hints=hints or DEFAULT_HINTS, dump_dir=dump_dir)
    return settle_with(ctrl, labels=labels or DEFAULT_LABELS, deny=deny or DEFAULT_DENY,
                       budget=budget, settle_timeout=settle_timeout)


# ---------------- 异步绑定（SAU 的 vendored 上传器用） ----------------

async def settle_async(page, *, hints=None, labels=None, deny=None, budget=DEFAULT_BUDGET,
                       settle_timeout=DEFAULT_SETTLE_TIMEOUT, dump_dir=None):
    """异步入口（微博等 vendored 上传器用 —— 它们是 async playwright）。

    ★ 为什么这里把回路抄了一遍（约 30 行）而不是共用 `settle_with`：
      playwright 的同步/异步是两套 API，同步函数里没法出现 `await`；共用只能靠
      "意图生成器 + 两个驱动器"那类间接层，为 30 行引入它不划算。
      ⇒ 代价是两边**必须同步演进**，所以 test_25 判据 16 做了结构性断言：
        异步回路也必须调 `pick_label` 且引用同一套 `DEFAULT_*` 常量。
    """
    hints = tuple(hints or DEFAULT_HINTS)
    labels = tuple(labels or DEFAULT_LABELS)
    deny = tuple(deny or DEFAULT_DENY)
    budget = max(1, int(budget))
    clicked, skipped, rounds = [], 0, 0
    reason = ""

    async def visible():
        out = []
        for sel in hints:
            try:
                n = await page.locator(sel).count()
            except Exception:                            # noqa: BLE001
                continue
            for i in range(min(n, SyncPlaywrightCtrl.PER_SEL_CAP)):
                out.append((sel, i))
        return out

    async def texts_of(dlg):
        sel, i = dlg
        try:
            loc = page.locator(sel).nth(i).locator(BUTTON_SEL)
            n = await loc.count()
            out = []
            for j in range(min(n, SyncPlaywrightCtrl.PER_BTN_CAP)):
                try:
                    out.append((await loc.nth(j).inner_text(timeout=2000) or "").strip())
                except Exception:                        # noqa: BLE001
                    out.append("")
            return out
        except Exception:                                # noqa: BLE001
            return []

    async def click(dlg, text):
        sel, i = dlg
        try:
            loc = page.locator(sel).nth(i).locator(BUTTON_SEL)
            n = await loc.count()
            for j in range(min(n, SyncPlaywrightCtrl.PER_BTN_CAP)):
                try:
                    if (await loc.nth(j).inner_text(timeout=2000) or "").strip() == text:
                        await loc.nth(j).click(timeout=5000)
                        return True
                except Exception:                        # noqa: BLE001
                    continue
        except Exception:                                # noqa: BLE001
            return False
        return False

    async def mark(dlg):
        """记下"要点的那个层"的**元素句柄**（与同步版同一套语义，见它的说明）。"""
        sel, i = dlg
        try:
            return await page.locator(sel).nth(i).element_handle()
        except Exception:                                # noqa: BLE001
            return None

    async def wait_changed(before_count, before_texts, timeout, handle):
        """等"这一步真的推进了"—— 三信号任一命中即算（模块 docstring 有说明）。

        ★ 与同步版逐条对应：count 降 / 句柄不可见 / **目标自己**的按钮文案变了。
        """
        end = time.monotonic() + max(0.2, float(timeout))
        while True:
            try:
                if len(await visible()) < before_count:
                    return True
                if handle is not None:
                    if not await handle.is_visible():
                        return True
                    now = []
                    for el in (await handle.query_selector_all(BUTTON_SEL) or []):
                        try:
                            now.append((await el.inner_text() or "").strip())
                        except Exception:                # noqa: BLE001
                            now.append("")
                    if [t for t in now if t] != [t for t in before_texts if t]:
                        return True
            except Exception:                            # noqa: BLE001
                return True
            if time.monotonic() >= end:
                return False
            await asyncio.sleep(0.2)

    while rounds < budget:
        dlgs = await visible()
        if not dlgs:
            break
        # ★ 与同步版同一套改动：候选 = **所有**有白名单按钮的层，逐层试（理由见同步版注释）
        cands = [d for d in reversed(dlgs)
                 if pick_label(await texts_of(d), labels, deny) is not None]
        if not cands:
            skipped = len(dlgs)
            break
        progressed, seen = False, []
        for target in cands:
            if progressed or rounds >= budget:
                break
            texts = await texts_of(target)
            tried = []
            while rounds < budget:
                lb = pick_label([t for t in texts if t.strip() not in tried], labels, deny)
                if lb is None:
                    break
                before = len(await visible())
                _marked_ok = await mark(target)
                if not await click(target, lb):
                    tried.append(lb)
                    seen.append(lb)
                    continue
                rounds += 1
                if await wait_changed(before, texts, settle_timeout, _marked_ok):
                    clicked.append(lb)
                    progressed = True
                    break
                tried.append(lb)
                seen.append(lb)
        if not progressed:
            reason = "所有可点的层都试过了、都没推进（试过：%s）" % ("、".join(seen) or "无白名单命中")
            break

    if not reason and rounds >= budget:
        dlgs = await visible()
        todo = [d for d in dlgs if pick_label(await texts_of(d), labels, deny) is not None]
        if todo:
            reason = "轮次用尽（budget=%d，还剩 %d 个可收的层）" % (budget, len(todo))
        else:
            skipped = len(dlgs)

    remaining = len(await visible())
    if reason:
        stuck = [reason]
        note = "弹层收不动：%s（已点：%s；可见层 %d）" % (reason, "、".join(clicked) or "无", remaining)
        # ★ page.url 要护住（2026-09-29 复查 finding 6a）：页面被关掉时读 url 会抛，
        #   而那时**恰恰最需要**落盘证据 —— 不能因为取个 url 把证据弄丢。同步版一直有这层保护。
        try:
            _url = page.url
        except Exception:                                # noqa: BLE001
            _url = ""
        _layers = []
        try:
            for _s, _i in await visible():
                _layers.append({"sel": _s, "idx": _i,
                                "buttons": (await texts_of((_s, _i)))[:12]})
        except Exception:                                # noqa: BLE001
            pass
        path = await dump_settle_failure_async(
            page, {"note": note, "rounds": rounds, "clicked": clicked, "stuck": stuck,
                   "remaining": remaining, "skipped": skipped, "url": _url,
                   "layers": _layers}, dump_dir)
        return {"ok": False, "rounds": rounds, "clicked": clicked, "stuck": stuck,
                "remaining": remaining, "skipped": skipped, "note": note, "dump": path}

    note = "%s（%d 轮，弹层已清空%s）" % (
        " → ".join(clicked) or "本来就没有可收的弹层", rounds,
        "" if not skipped else "；放过 %d 个无可点按钮的层" % skipped)
    return {"ok": True, "rounds": rounds, "clicked": clicked, "stuck": [],
            "remaining": remaining, "skipped": skipped, "note": note, "dump": None}


# ---------------- 遮挡检查（"点发布前"的硬闸） ----------------

_JS_OCCLUDED = """
([x, y, hints]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return "\\u5750\\u6807\\u4e0a\\u6ca1\\u6709\\u5143\\u7d20";
  // ★ 命中点就在**目标自己**（或它的后代）里 ⇒ 目标就在最上面，没被挡住。
  //   2026-09-29 复查 finding 3：早先无条件沿祖先链走到 <html>，于是
  //   "目标本身住在一个 class 含 layer/dialog 的容器里"（微博主表单就是 div.wbpro-layer）
  //   会被误判成"被浮层挡住"——正是 §2.5 写出来要避免的那个误报。
  if (el.closest('[data-settle-target]')) return "";
  let n = el;
  while (n && n.tagName) {
    for (const h of hints) {
      try { if (n.matches(h)) {
        const c = (typeof n.className === 'string') ? n.className : '';
        return (n.tagName.toLowerCase() + '.' + c).slice(0, 80);
      } } catch (e) { }
    }
    n = n.parentElement;
  }
  return "";
}
"""


def is_occluded_by_layer(page, locator, hints=DEFAULT_HINTS):
    """目标元素中心点被浮层压着没有 → (挡住?, 说明)。

    ★ 为什么用"遮挡检查"而不是"有没有弹层"（设计 §2.5）：
      微博的**主表单本身就是** `div.wbpro-layer` —— 按"有没有层"判会把正常页面
      判成"挡住"。真正会坏事的是"我要点的那个按钮中心点被浮层压着"。
    ★ 命中点落在**目标自己（或其后代）**里 ⇒ 一律算"没挡住"（2026-09-29 复查 finding 3）：
      否则"目标住在 layer/dialog 容器里"这种常见结构会被自己的祖先误判挡住 ——
      那就又回到 §2.5 要避免的误报了。
    ★ hints 里的 `:visible` **必须**去掉再进浏览器：那是 playwright 伪类，
      浏览器原生 `element.matches()` 认不得，会直接抛异常。
    ★ **两条失败路径的方向是故意不同的**（2026-09-29 OCR 复查指出这里没说清）：
      · **坐标上取不到元素 / 目标没有包围盒**（元素真不在那儿）→ 判**"挡住"**（保守：宁可不点）；
      · **JS 抛异常 / 打不上临时标记**（我们自己的检查坏了）→ 判**"没挡住"**（放行）——
        那是改动前的既有行为，不让"检查工具的故障"变成"发布失败"的新原因。
      两者都在返回值第二项里留了说明，调用方据此写进 note/steps。
    """
    try:
        box = locator.bounding_box()
    except Exception:            # noqa: BLE001
        return True, "取不到目标包围盒"
    if not box or box.get("width", 0) < 1 or box.get("height", 0) < 1:
        return True, "目标不可见（无包围盒）"
    x = box["x"] + box["width"] / 2.0
    y = box["y"] + box["height"] / 2.0
    plain = [h.replace(":visible", "") for h in (hints or DEFAULT_HINTS)]
    # 给目标打个**临时**标记，好让 JS 认出"命中点就是目标自己"；用完立刻摘掉
    # （2026-09-29 复查 finding 3：残留会让 hold.html 那类整页落盘带上无关属性）。
    try:
        locator.evaluate("el => el.setAttribute('data-settle-target','1')")
    except Exception:            # noqa: BLE001
        return False, "标记目标失败（按未挡住处理）"
    try:
        hit = page.evaluate(_JS_OCCLUDED, [x, y, plain])
    except Exception as e:       # noqa: BLE001
        return False, "遮挡检查失败（按未挡住处理）：%s" % str(e)[:60]
    finally:
        try:
            locator.evaluate("el => el.removeAttribute('data-settle-target')")
        except Exception:        # noqa: BLE001
            pass
    return (True, "被浮层挡住：%s" % hit) if hit else (False, "未被遮挡")

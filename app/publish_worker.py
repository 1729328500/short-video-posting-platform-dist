# -*- coding: utf-8 -*-
"""单平台发布工作进程（由 server.py 发布引擎拉起）

★★ v1.2 行为变更（用户 2026-09-27 拍板）：

  旧版：打开创作页 → 把视频投给 input[type=file] → sleep 4 秒 → **关掉窗口** →
        状态写「请人工确认发布」。窗口都关了，用户无从确认，上传还被中断。
  新版：**填好为止，停在提交前**——
        打开发布页 → 检测登录（复用 platform_login，同一套判据）→ 投递视频 →
        等表单出现 → 填标题 → 填正文+话题 → 截图存档 →
        状态落「待你确认提交」→ **窗口保持打开**，等用户自己点提交/关窗。

  ★ 提交按钮由人工点，本进程不碰。这样在适配器还没逐平台校准的阶段，
    不会出现「自动化点错了还撤不回来」的事故。

各平台选择器来自 publish_adapters.json（平台改版只改配置，不改代码）。
"""
import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from platform_login import classify  # noqa: E402
import browser_channel  # noqa: E402
import browser_state  # noqa: E402
import sau_bridge  # noqa: E402
import dialog_settle   # noqa: E402  ★ 弹窗收尾：见 10-弹窗收尾-设计.md

ADAPTERS_PATH = APP_DIR / "publish_adapters.json"


def w(path, obj):
    obj = dict(obj)
    obj["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        Path(path).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_adapter(key):
    """读适配器配置。

    ★ 支持 `inherits`：某些平台与另一个平台**共用创作后台**（实测西瓜视频
      studio.ixigua.com 与 /upload 全部 302 到 creator.douyin.com），
      这时只需要写 `"inherits": "douyin"` + 覆盖少数几项即可，
      不必复制一整份选择器 —— 父平台校准好了，子平台自动跟着对。
    """
    try:
        data = json.loads(ADAPTERS_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    plats = data.get("platforms") or {}
    d = dict(data.get("defaults") or {})
    p = dict(plats.get(key) or {})
    parent = p.get("inherits")
    if parent and parent in plats:
        d.update({k: v for k, v in (plats[parent] or {}).items() if v not in (None, "")})
    d.update({k: v for k, v in p.items() if v not in (None, "")})
    return d


def first_visible(page, selectors):
    """按顺序试选择器，返回第一个能匹配到的 locator（不要求可见——
    发布页的文件输入常常是隐藏的，靠按钮触发，但 set_input_files 对它照样有效）"""
    for sel in selectors:
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                return loc, sel
        except Exception:
            continue
    return None, ""


# 封面入口的通用候选（按顺序试）。
# ★ 为什么锚在「封面」这个词上（2026-09-28 用户实测：头条/B站封面没自动填入）：
#   各平台的入口文案不一样（设置封面 / 更换封面 / 编辑封面 …），但**都带「封面」二字**。
#   平台改版改的是 class 名，中文文案反而稳。适配器里若配了 cover_trigger 就优先用它。
#   顺序：先具体后宽泛 —— 越往后越可能误点，所以放在后面。
# ★ 顺序讲究：「添加/设置/更换/编辑封面」这类在**主表单**上，点了**打开弹层**；
#   「上传封面」在**弹层里面**，弹层没开时点它没用。
#   实测（2026-09-28，拿 B站发布页的真实 HTML 比对）：
#     添加封面 → div.cover-main < div.cover < div.form-item < div.form   ← 主表单
#     上传封面 → ... < div.bcc-dialog                                      ← 弹层内
#   所以把「上传封面」放到最后。
DEFAULT_COVER_TRIGGERS = (
    "text=添加封面", "text=设置封面", "text=更换封面", "text=编辑封面",
    "text=选择封面", "text=修改封面", "text=封面设置",
    "text=上传封面",                      # 弹层内 —— 万一有些平台直接就是它，仍留着兜底
    "a:has-text('封面')", "button:has-text('封面')",
    "span:has-text('封面')", "div:has-text('封面')",
)

# 判定「弹层开没开」用的容器特征。
# ★ 为什么不能只靠"找到图片文件框"（2026-09-28 实测教训）：
#   封面的图片 file input 在**弹层没打开时也存在于 DOM 里**（只是 display:none），
#   所以「找得到」根本不能证明入口点对了 —— 会把「点了没反应」误报成「封面已上传」。
#   换个能分辨的信号：点完之后**有没有可见的弹层/对话框容器出现**。
# ★ 2026-09-29：改引用 dialog_settle，避免两处各维护一份（改一处忘一处就是坑 5 的翻版）
DIALOG_HINTS = dialog_settle.DEFAULT_HINTS

# 有些平台的封面上传是**两步**：先点入口弹出选择层，再点层里的「本地上传」才出文件框。
# 实测头条号（2026-09-28，拿它发布页的真实 HTML 比对）：点「上传封面」后出现
# `div.m-xigua-dialog`，里面是「封面截取 / 本地上传」—— 而此刻页面上**一个图片型
# file input 都没有**（只有视频的），必须再点一下「本地上传」才会创建。
COVER_SUB_TRIGGERS = (
    "text=本地上传", "text=上传图片", "text=本地图片",
    "text=点击上传", "text=选择图片", "text=从本地上传", "text=本地上传图片",
)

# 封面文件框只认**图片**型的 accept。
# ★ 绝不能兜底成 `input[type=file]` —— 发布页上那个裸 file input 是**视频**入口，
#   把封面图塞进去轻则报错、重则把视频位顶掉。
COVER_FILE_INPUTS = (
    "input[type=file][accept*='image']",
    "input[type=file][accept*='jpg']",
    "input[type=file][accept*='jpeg']",
    "input[type=file][accept*='png']",
)


def _find_cover_file_input(page, ad):
    """找封面用的图片文件框；找不到返回 None（**不猜**）"""
    sels = [ad.get("cover_file_input")] + list(COVER_FILE_INPUTS)
    for s in sels:
        if not s:
            continue
        try:
            loc = page.locator(s).first
            if loc.count() > 0:
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def set_cover(page, ad, thumb, steps, dump_dir=None):
    """把用户的封面图投进平台的封面位。

    做法是「试 + 验」而不是「一次点准」：逐个候选入口去点，点完去找**图片文件框** ——
    找得到就说明入口找对了，找不到就按 Esc 退回、换下一个候选。
    这样平台把文案或结构改一改，最多是这里退化成「没设上」，不至于点坏别的东西。
    """
    cands = ([ad.get("cover_trigger")] if ad.get("cover_trigger") else []) + list(DEFAULT_COVER_TRIGGERS)
    tried = []
    for sel in cands:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0 or not loc.is_visible():
                continue
            tried.append(sel)
            before = _visible_dialog_count(page)
            try:
                loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:  # noqa: BLE001
                pass
            loc.click(timeout=6000)
            _wait_more_dialogs(page, before, ad.get("wait_after_cover_trigger", 2))
            opened = _visible_dialog_count(page) > before

            fin = _find_cover_file_input(page, ad)
            if fin is None:
                # 两步式：入口弹出了选择层，还得再点一下「本地上传」才出文件框（头条号）
                subs = ([ad.get("cover_subtrigger")] if ad.get("cover_subtrigger") else []) \
                    + list(COVER_SUB_TRIGGERS)
                for sub in subs:
                    try:
                        sl = page.locator(sub).first
                        if sl.count() == 0 or not sl.is_visible():
                            continue
                        sl.click(timeout=6000)
                        _wait_cover_input(page, ad, ad.get("wait_after_cover_subtrigger", 2))
                        fin = _find_cover_file_input(page, ad)
                        if fin is not None:
                            sel = "%s → %s" % (sel, sub)
                            break
                    except Exception:  # noqa: BLE001
                        continue
            if fin is None:
                try:
                    page.keyboard.press("Escape")     # 没弹层/没文件框 → 退回，换下一个
                except Exception:  # noqa: BLE001
                    pass
                # before 是**点之前**那个基准（本轮开头记的）；Escape 之后等它回到 before
                _wait_fewer_dialogs(page, before + 1, 0.8)
                continue

            fin.set_input_files(thumb, timeout=20000)
            if not _wait_cover_preview(page, ad.get("wait_after_cover_upload", 6)):
                steps.append("封面投递后没等到预览图（继续，留痕）")

            cok = ad.get("cover_confirm")
            if cok:
                cb = first_visible(page, [cok])[0]
                if cb is not None:
                    try:
                        cb.click(timeout=8000)
                    except Exception:  # noqa: BLE001
                        pass
            # ★ 2026-09-29：**不管有没有配 cover_confirm**，都跑一次统一收尾。
            #   老写法只在"没配 cover_confirm"时才走兜底 → 头条一配上就绕过了兜底循环，
            #   于是"确定之后还有二次确认"没人管（设计 §1.1 缺陷④）。
            r = dialog_settle.settle_sync(page, dump_dir=dump_dir,
                                          labels=ad.get("modal_labels"))
            if r["clicked"]:
                steps.append("封面后弹层收尾：%s" % r["note"])
            if not r["ok"]:
                steps.append("⚠️ 封面后弹层收不动（已落盘 %s）：%s"
                             % (r["dump"] or "失败", r["note"]))
            # 如实说明：弹层开出来了才敢说入口点对了；
            # 没开也照样投了（有些平台封面就摆在表单上、没有弹层），但标注出来供人工核对。
            steps.append("封面已投递（入口：%s%s）"
                         % (sel, "" if opened else "，未确认弹层——请核对"))
            return True
        except Exception as e:  # noqa: BLE001
            # ★ 异常要记进 steps（2026-09-29 复查 finding 1 的教训）：原来这里什么都不记，
            #   于是"代码里的 bug"（例如引用了不存在的变量）会被**报成配置问题**——
            #   "没找到封面入口…文案可能又变了"，把排查方向带偏。
            steps.append("封面候选 %s 出错（换下一个）：%s" % (sel, str(e)[:80]))
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            continue
    steps.append("没找到封面入口（试过：%s；文案可能又变了，需要重新标定）"
                 % ("、".join(tried[:4]) or "无候选可见"))
    return False


# ★ 2026-09-29：老的那套封面弹层兜底**已整体删除**（它"点一次就 return True"，
#   遇到二次确认必漏，而且点完不验证层真关了 —— 设计 §1.1 列了三个缺陷）。
#   现在统一交给 `dialog_settle.settle_sync()`（见封面那段的调用）。
#   它原来的注释留在这里，因为那是**实测结论**、仍然有效：
#   「上传封面后弹层不会自动关闭，它停在页面中间，把下半截表单挡住 ——
#     表现为「标题填上了、正文点不动」（`正文填写失败：Locator.click: Timeout`）」
#   「只在可见的弹层内部找按钮：表单上也可能有同名的「确定」，全局找会点错」


def _visible_dialog_count(page):
    """当前有多少个可见的弹层/对话框容器（用来判断「点完之后弹层开没开」）"""
    n = 0
    for sel in DIALOG_HINTS:
        try:
            n += page.locator(sel).count()
        except Exception:  # noqa: BLE001
            pass
    return n


def _settle_dump_dir(args):
    """弹窗收尾的落盘目录：<data>/dialogs/（与 publish/、sau/ 同级）。"""
    try:
        return Path(args.status).parent.parent / "dialogs"
    except Exception:            # noqa: BLE001
        return None


def _wait_more_dialogs(page, before, cap):
    """等"可见弹层数 > before"，最多 cap 秒。

    ★ `wait_after_*` 从"就等这么久"**降级为兜底上限**（设计 §5）：先等结果，
      等不到才耗到上限 —— 慢的时候不再白等，快的时候不再超时。
    """
    end = time.monotonic() + max(0.3, float(cap or 2))
    while time.monotonic() < end:
        if _visible_dialog_count(page) > before:
            return True
        time.sleep(0.2)
    return False


def _wait_fewer_dialogs(page, before, cap):
    """等"可见弹层数 < before"（点了「取消」/按了 Escape 之后等层收回去），最多 cap 秒。"""
    end = time.monotonic() + max(0.2, float(cap or 0.8))
    while time.monotonic() < end:
        if _visible_dialog_count(page) < before:
            return True
        time.sleep(0.2)
    return False


def _wait_cover_input(page, ad, cap):
    """等封面文件框出现（两步式封面的第二步之后才有），最多 cap 秒。"""
    end = time.monotonic() + max(0.3, float(cap or 2))
    while time.monotonic() < end:
        if _find_cover_file_input(page, ad) is not None:
            return True
        time.sleep(0.2)
    return False


def _wait_cover_preview(page, cap):
    """等封面预览/裁切图出现（有就说明投递生效了），最多 cap 秒。

    ★ 只是"更好"的等待，**不是**判定成功的依据：没等到也照旧往下走（与改动前一致），
      但日志里能看出"没等到预览"。
    """
    sels = (".cropper-container img[src^='blob:']", "img[src^='blob:']",
            "[class*='cover'] img[src^='data:']")
    end = time.monotonic() + max(0.5, float(cap or 6))
    while time.monotonic() < end:
        for s in sels:
            try:
                if page.locator(s).count():
                    return True
            except Exception:    # noqa: BLE001
                continue
        time.sleep(0.2)
    return False


def _blocked_submit_status(name, why, submit):
    """提交按钮被浮层挡住时，该写什么 state / note（返回二元组）。

    ★ 为什么要单独抽出来（2026-09-29 独立复查 finding 2）：
      ① 原来只往 `steps` 里写了一句，而**界面不渲染 steps**（它只显示 note/error）
         → 用户在停手模式下**完全看不到"按钮被挡住"**，照着提示"自己点提交"，
         正好点在一个遮罩上；
      ② `--submit` **2026-09-29 起应用一律传**（用户决定全部自动发布）⇒ 上面那条
         "硬失败"分支**就是生产路径**（不再是防御性代码）；停手分支仍保留，
         只有手工跑 publish_worker 不带 `--submit` 时才走。
    """
    if submit:
        return "fail", ("%s：提交按钮点不到（%s），**未点提交**——本条判失败。"
                        "现场已落盘，请先处理再重试。" % (name, why))
    return "manual", ("%s：已填好，**停在提交前**——但 ⚠️ 提交按钮点不到（%s）："
                      "请先在页面上把挡住的弹层关掉，再核对提交。" % (name, why))


def _select_declaration(page, ad, steps, option_text):
    """选「创作声明」这类下拉（B站必填）。**选完回读输入框**当自证。

    ★ 2026-09-29 真机（用户："B站你点提交的时候会让你选创作声明，还没选就一直卡在哪里"）：
      它是表单里一个 `bcc-select` 下拉（DOM 见 job_53.hold.html），不是按钮 ——
      所以"收弹层"那套点不到它；不选就点【立即投稿】毫无反应（job_53 就这样变成 manual）。
    """
    trig = ad.get("declaration_trigger")
    if not trig:
        return False
    try:
        loc, used = first_visible(page, [trig])
        if loc is None:
            steps.append("没找到创作声明入口（%s）——请人工选一下「%s」" % (trig, option_text))
            return False
        before = _dec_value(page)
        loc.click(timeout=8000)
        time.sleep(0.6)                      # 等下拉展开（列表本来就在 DOM 里，但要等可见）
        # ★ 选项怎么找（2026-09-29 OCR 复查 #1）：`:has()` 里**只能放纯 CSS** ——
        #   `li.bcc-option:has(span:text-is("X"))` 里的 `text-is()` 是 Playwright 私有伪类，
        #   放进去**匹配不到任何东西**（功能会静默失效）。改用 CSS 选到全部选项 +
        #   用**整串精确**的正则过滤（与项目「坑 6」一致：不用子串匹配）。
        opt = page.locator("li.bcc-option").filter(
            has_text=re.compile("^" + re.escape(option_text) + "$")).first
        if not _loc_exists(opt):
            steps.append("创作声明下拉里没找到「%s」——请人工选" % option_text)
            return False
        opt.click(timeout=6000)
        time.sleep(0.5)
        after = _dec_value(page)
        if after and option_text in after:
            # ★ `after != before` 不能当必要条件（2026-09-29 OCR 复查 #2）：
            #   若**本来就已选中**，after == before，会被误报成"没选上"。
            if after == before:
                steps.append("创作声明本来就已是「%s」（入口 %s）" % (after, used))
            else:
                steps.append("创作声明已选：%s（入口 %s）" % (after, used))
            return True
        steps.append("⚠️ 创作声明似乎没选上（输入框现在显示 %r，之前 %r）—— 请人工核对"
                     % (after, before))
        return False
    except Exception as e:                   # noqa: BLE001
        steps.append("选创作声明出错：%s（请人工选一下）" % str(e)[:60])
        return False


def _dec_value(page):
    """回读创作声明输入框当前显示的值（空＝还没选）——用来**自证**选上了没有。"""
    for sel in ("div.statement-content input.bcc-select-input-inner",
                "div.statement-content input[readonly]"):
        try:
            v = page.locator(sel).first.input_value(timeout=3000)
            if v:
                return str(v).strip()
        except Exception:            # noqa: BLE001
            continue
    return ""


def _loc_exists(loc):
    """locator 是否存在（同步 API 的 count()）。★ 名字别带 await —— 这是同步的。"""
    try:
        return loc.count() > 0
    except Exception:                # noqa: BLE001
        return False


def _resolve_submit_button(page, ad):
    """找"提交按钮"那个元素 —— **滚动和硬闸必须用同一套**（2026-09-29 真机教训）。

    ★ 实测（job 48 哔哩哔哩）：滚动那步用的是 `get_by_role("button", name=submit_text)`，
      而它的按钮是 `<span class="submit-add">立即投稿</span>` —— **不是 `<button>`**
      ⇒ 滚动那步找不到 ⇒ 按钮留在视口外 ⇒ 到了硬闸，`elementFromPoint` 在视口外取不到元素
      ⇒ 判"点不到" ⇒ 整条失败。两处只要有一处认对，那次就不会失败 —— 所以统一到这里。
    返回 (locator, 说明)；找不到返回 (None, 说明)。
    """
    bsel = ad.get("submit_button")
    if bsel:
        loc, used = first_visible(page, [bsel])
        if loc is not None:
            return loc, used
    txt = ad.get("submit_text", "发布")
    err = ""
    try:
        cand = page.get_by_role("button", name=txt, exact=True)
        if cand.count():
            return (cand.last if cand.count() > 1 else cand.first), "role=%s" % txt
    except Exception as e:       # noqa: BLE001
        # ★ 不能 `pass`（2026-09-29 OCR 复查指出）：那是**把真错误伪装成"没找到"**——
        #   页面已关/选择器语法坏这类问题会被报成"配置不对"，排查方向直接带偏。
        #   （同一个毛病上一轮在 set_cover 里也犯过：NameError 被吞成"文案可能又变了"。）
        err = "；查 role 时出错：%s" % str(e)[:60]
    return None, "没找到（submit_button=%s / submit_text=%s）%s" % (bool(bsel), txt, err)


def _settle_before_step(page, args, steps, where, ad=None):
    """一步之前先把残留弹层收掉。

    ★ 失败语义照设计 §3.1：收不动**不终止**（后面的步骤还有可能成），但要
      ①落盘 ②在步骤里如实写 —— 免得错误又飘成很远处的"正文点不动"。
    ★ `ad` 里的 `modal_labels` 会加进白名单（2026-09-29 实测：头条/西瓜的封面弹窗
      真实按钮是「**完成裁剪**」，而精确匹配下 `完成裁剪 ≠ 完成` ⇒ 白名单里加它）。
    """
    r = dialog_settle.settle_sync(page, dump_dir=_settle_dump_dir(args),
                                  labels=(ad or {}).get("modal_labels"))
    if r["clicked"] or not r["ok"]:
        steps.append("%s弹层收尾：%s" % (where, r["note"]))
    if not r["ok"]:
        steps.append("⚠️ %s有弹层收不动（已落盘 %s）" % (where, r["dump"] or "失败"))
    return r


def dump_hold_snapshot(page, status_path):
    """停在提交前时把现场落盘，供**标定选择器**用。

    ★ 为什么要它（2026-09-28）：头条/B站的封面入口**要上传视频之后才出现在页面上**，
      不上传就没法标定，而我又不能往用户账号里真发视频。
      这套流程本来就会停在提交前、并保持窗口打开 —— 那一瞬间页面就是标定所需的现场。
      所以在这里把「页面结构摘要 + 整页 HTML」写下来，我读文件就能精确标定，
      不用靠猜、也不用反复让用户试。

    摘要是刻意的：整页 HTML 动辄几百 KB，而真正需要的只是
    「哪些元素带封面/标题/上传字样、各自的层级路径、以及所有 file 框的 accept」。
    """
    p = Path(status_path)
    out = {"url": "", "title": "", "candidates": [], "file_inputs": []}
    try:
        out["url"] = page.url
        out["title"] = page.title()
    except Exception:  # noqa: BLE001
        pass
    try:
        out["candidates"] = page.evaluate("""() => {
            const out=[], seen=new Set();
            document.querySelectorAll('a,button,div,span,label,p').forEach(e=>{
                const t=(e.innerText||'').trim();
                if(!t||t.length>16)return;
                if(!/封面|标题|上传|发布|下一步/.test(t))return;
                const r=e.getBoundingClientRect();
                if(r.width<2||r.height<2)return;
                const k=t+'|'+Math.round(r.x)+','+Math.round(r.y);
                if(seen.has(k))return; seen.add(k);
                let n=e,path=[];
                for(let i=0;i<5&&n&&n.tagName;i++){
                    let s=n.tagName.toLowerCase();
                    if(n.id)s+='#'+n.id;
                    else if(n.className&&typeof n.className==='string')
                        s+='.'+n.className.trim().split(/[ ]+/).slice(0,2).join('.');
                    path.unshift(s); n=n.parentElement;
                }
                out.push({t, x:Math.round(r.x), y:Math.round(r.y),
                          w:Math.round(r.width), h:Math.round(r.height),
                          p:path.join(' > ').slice(0,200)});
            });
            return out.slice(0,60);
        }""")
    except Exception:  # noqa: BLE001
        pass
    try:
        out["file_inputs"] = page.evaluate("""() => Array.from(
            document.querySelectorAll('input[type=file]')).map(e=>({
                accept:(e.getAttribute('accept')||''),
                cls:(typeof e.className==='string'?e.className:'').slice(0,60),
                parent:(e.parentElement&&e.parentElement.className||'').slice(0,60),
                visible: e.getBoundingClientRect().width>0}))""")
    except Exception:  # noqa: BLE001
        pass
    try:
        p.with_suffix(".hold.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    try:
        p.with_suffix(".hold.html").write_text(page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def compose_body(body, topics):
    body = (body or "").strip()
    topics = (topics or "").strip()
    if not topics:
        return body
    return (body + "\n" + topics).strip() if body else topics


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--url", default="")
    ap.add_argument("--profile", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--body", default="")
    ap.add_argument("--topics", default="")
    ap.add_argument("--status", required=True)
    ap.add_argument("--thumbnail", default="",
                    help="封面图路径（用户在素材页上传/截取的那张）。"
                         "★ 微博是硬性要求，没有它微博必失败")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--hold", type=int, default=900, help="填完后保持窗口打开的秒数")
    ap.add_argument("--submit", action="store_true",
                    help="填完后自动点提交。★ 默认不点 —— 不带这个参数就停在提交前等人工")
    ap.add_argument("--engine", default="auto", choices=("auto", "sau", "legacy"),
                    help="发布引擎：auto=能用 social-auto-upload 就用 / sau=强制 / legacy=本项目手写那套")
    ap.add_argument("--visibility", default="keep",
                    help="可见性：keep(默认不动) / public / friends / private")
    args = ap.parse_args()

    ad = load_adapter(args.key)
    name = ad.get("name") or args.key

    # 共用创作后台的平台复用同一个登录 profile（如西瓜视频 = 抖音后台）
    pk = ad.get("profile_key")
    if pk:
        prof = Path(args.profile)
        if prof.name != pk:
            args.profile = str(prof.parent / pk)
            w(args.status, {"state": "running",
                            "note": "%s 与 %s 共用登录态，改用后者的 profile" % (name, pk)})

    # ── 引擎选择：有 social-auto-upload 上传器的平台默认走它 ──
    #    auto   = 能用 SAU 就用，否则回落本项目手写那套
    #    sau    = 只用 SAU（不支持就报错）
    #    legacy = 强制用本项目手写那套（调试/对比用）
    if args.engine in ("auto", "sau") and sau_bridge.supports(args.key):
        return run_via_sau(args, ad, name)
    if args.engine == "sau":
        w(args.status, {"state": "fail",
                        "note": "%s 没有 social-auto-upload 上传器" % name})
        sys.exit(1)

    publish_url = args.url or ad.get("publish_url") or ""
    if not publish_url:
        w(args.status, {"state": "fail", "note": "没有配置 %s 的发布页地址" % name})
        sys.exit(1)

    w(args.status, {"state": "running", "note": "打开%s发布页" % name})

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        w(args.status, {"state": "fail", "note": "未安装 playwright：pip install playwright（%s）" % e})
        sys.exit(1)

    steps = []
    try:
        with sync_playwright() as p:
            with browser_state.persistent_context(p.chromium,
                user_data_dir=args.profile, channel=browser_channel.channel(),
                headless=args.headless,
                viewport={"width": 1440, "height": 900}) as ctx:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()

                # ── 先走「入口」：有些平台的发布页在**左侧导航的下拉菜单**里，
                #    直接开 publish_url 要么落到首页、要么表单不挂载（SPA 只渲染空壳）。
                #    这是用户实测反馈的（头条号 / 哔哩哔哩），和 vendored 的 SAU 在视频号
                #    上踩过的是同一类 —— 上游注释写着「正确入口：先进首页，再点可见的
                #    『发表视频』按钮做客户端跳转，表单才会真正挂载」。
                #    适配器配了 entry_url 就走这条路：落地 →（可选悬停展开下拉）→ 点入口。
                #    没配就还是原来的「直接开 publish_url」，对已有平台零影响。
                entry_url = ad.get("entry_url") or publish_url
                try:
                    page.goto(entry_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    steps.append("goto 超时（继续）：%s" % str(e)[:40])
                time.sleep(ad.get("wait_after_goto", 6))

                if ad.get("entry_click"):
                    # ★ 重试而不是只试一次（2026-09-28 B站实测）：
                    #   这些页面是 SPA，导航栏**渲染得比 domcontentloaded 晚**。
                    #   只 hover 一次就点，会撞上「元素还不存在/不可点」——
                    #   `Locator.click: Timeout 10000ms exceeded`，然后白白回退到直接开发布页
                    #   （而那条路对 B站 是没用的：它的表单要客户端跳转才挂载）。
                    #   改成：等导航栏出现 → 悬停 → 点，失败就重试，最多 4 轮。
                    hv = ad.get("entry_hover")
                    ok_entry = False
                    last_err = ""
                    for attempt in range(4):
                        try:
                            if hv:
                                page.wait_for_selector(hv, state="visible", timeout=15000)
                                page.locator(hv).first.hover(timeout=10000)
                                time.sleep(ad.get("wait_after_entry_hover", 2.5))
                            page.locator(ad["entry_click"]).first.click(timeout=12000)
                            ok_entry = True
                            break
                        except Exception as e:  # noqa: BLE001
                            last_err = str(e)[:70]
                            time.sleep(2.5)          # 等页面再渲染一会儿，再来一轮
                    if ok_entry:
                        time.sleep(ad.get("wait_after_entry_click", 6))
                        steps.append("已从导航入口进入发布页（%s）" % page.url[:60])
                    else:
                        steps.append("走导航入口失败（试了 4 轮）：%s" % last_err)
                        try:
                            page.goto(publish_url, wait_until="domcontentloaded", timeout=60000)
                            time.sleep(ad.get("wait_after_goto", 6))
                        except Exception:  # noqa: BLE001
                            pass
                        # 直接开 URL 也给它一点渲染时间再往下走
                        time.sleep(3)
                page.screenshot(path=str(Path(args.status).with_suffix(".png")))

                # ── 登录检测：和登录窗同一套判据 ──
                txt = ""
                try:
                    txt = page.evaluate("document.body ? document.body.innerText : ''") or ""
                except Exception:
                    pass
                st, note, detail = classify(txt, page.url)
                if st == "waiting":
                    # reason 是给主程序看的机器可读字段：主程序据此把账号状态改成「未登录」。
                    # ★ 为什么需要：登录态会过期，但账号页此前只认「上次登录成功」这个事实，
                    #   会一直显示已登录 —— 用户点了发布才失败。实测踩过（2026-09-27）。
                    w(args.status, {"state": "fail", "reason": "not_logged_in",
                                    "note": "%s：像是没登录（%s），请先到账号页扫码" % (name, note)})
                    browser_state.close(ctx, args.profile)
                    return
                if st != "on":
                    # ★ 2026-10-06（安全审查 R9）：判不准不再中止发布 ——
                    #   收紧登录判据（见 platform_login.classify）不能反过来把真实发布挡住：
                    #   平台改版会让很多正常页面落进 unknown。继续投递，失败会在后续步骤
                    #   给出确凿信息（找不到上传入口会 hold 住窗口让用户手动操作）。
                    steps.append("登录态未能确认（%s），继续尝试投递" % note)
                else:
                    steps.append("登录态正常")

                # ── 投递视频 ──
                w(args.status, {"state": "running", "note": "上传视频到%s…" % name})
                loc, used = first_visible(page, [ad.get("file_input"), ad.get("file_input_fallback"),
                                                 "input[type=file][accept*='video']", "input[type=file]"])
                if loc is None:
                    w(args.status, {"state": "manual",
                                    "note": "%s：没找到上传入口（选择器可能过期），窗口已打开，可手动上传" % name,
                                    "steps": steps + ["未找到 file input"]})
                    _hold(ctx, page, args, steps, name)
                    return
                steps.append("上传入口=%s" % used)
                try:
                    loc.set_input_files(args.video, timeout=60000)
                except Exception as e:
                    w(args.status, {"state": "fail", "note": "%s 投递视频失败：%s" % (name, str(e)[:120])})
                    browser_state.close(ctx, args.profile)
                    return
                time.sleep(ad.get("wait_after_upload", 12))
                steps.append("视频已投递")

                # ── 封面（可选）──
                # 为什么要单独一步：头条号、微博这类平台**不带封面就不让发**
                # （实测 2026-09-28：微博直接 `raise ValueError("必须提供封面图")`）。
                # 素材页本来就支持用户上传/截取封面，这一步就是把那张图投进去。
                # 选择器走适配器配置（各平台入口长得不一样），**没配就整步跳过** ——
                # 所以对已经能发的平台没有任何影响。
                if args.thumbnail:
                    try:
                        if not set_cover(page, ad, args.thumbnail, steps,
                                         dump_dir=_settle_dump_dir(args)):
                            pass        # 失败原因已经写进 steps
                    except Exception as e:  # noqa: BLE001
                        # 封面失败**不中断**：有的平台封面是可选的，
                        # 不该因为它没设上就整条发不出去。
                        steps.append("封面上传出错（继续）：%s" % str(e)[:60])
                else:
                    steps.append("没设封面，跳过")

                # ── 填标题 ──
                _settle_before_step(page, args, steps, "填标题前", ad=ad)
                tloc, tused = first_visible(page, [ad.get("title_input"), "input[placeholder*='标题']"])
                if tloc is not None and args.title:
                    try:
                        tloc.fill(args.title, timeout=15000)
                        steps.append("标题已填(%s)" % tused)
                    except Exception as e:
                        steps.append("标题填写失败：%s" % str(e)[:60])
                else:
                    steps.append("未找到标题框" if args.title else "无标题内容")

                # ── 填正文 + 话题 ──
                _settle_before_step(page, args, steps, "填正文前", ad=ad)
                bl = compose_body(args.body, args.topics)
                eloc, bused = first_visible(page, [ad.get("body_editor"), "[contenteditable=true]", "textarea"])
                if eloc is not None and bl:
                    try:
                        eloc.click(timeout=10000)
                        page.keyboard.insert_text(bl)
                        steps.append("正文已填(%s)" % bused)
                    except Exception as e:
                        steps.append("正文填写失败：%s" % str(e)[:60])
                else:
                    steps.append("未找到正文框" if bl else "无正文内容")

                # ★ 真机校准（2026-09-27 抖音实测）：话题里的 `#` 会触发话题联想下拉框，
                #   盖住半个界面，用户误按回车还会插进平台推荐的话题。
                #   实测：press("Escape") **无效**（下拉照样在）；点一下标题框把焦点移走才关得掉，
                #   且编辑器里已输入的内容不会丢。
                try:
                    neutral = first_visible(page, [ad.get("title_input"), "input[placeholder*='标题']"])[0]
                    if neutral is not None:
                        neutral.click(timeout=6000)
                    else:
                        page.keyboard.press("Tab")
                    time.sleep(0.8)
                    steps.append("已移开焦点关闭话题联想下拉")
                except Exception as e:  # noqa: BLE001
                    steps.append("关闭话题下拉失败：%s" % str(e)[:50])

                # ── 创作声明（B站必填；2026-09-29 真机：不选就点不动【立即投稿】）──
                if ad.get("declaration_option"):
                    # ★ 用上返回值（2026-09-29 OCR 复查 #3）：丢了它的话，"声明没选上"和"没配声明"
                    #   在后续步骤里长得一模一样，而前者的后果是【提交静默无效】——
                    #   必须留一条一眼能看到的线，别让用户对着"已点提交但没读到成功标记"发懵。
                    if not _select_declaration(page, ad, steps, ad["declaration_option"]):
                        steps.append("⚠️ 创作声明没选上 —— 【%s】很可能点了也没反应，请先手工选好再提交"
                                     % ad.get("submit_text", "发布"))

                # ★ 真机校准：提交按钮在页面底部（抖音实测 y≈1277，远超视口），
                #   不滚过去用户根本找不到。只滚动，**绝不点击**。
                sub_text = ad.get("submit_text", "发布")
                try:
                    # ★ 与硬闸用**同一个**解析函数（见 _resolve_submit_button 的说明：
                    #   两处认的不是同一个元素时，就会出现"没滚到视野 → 硬闸判点不到"）
                    sub, _sub_how = _resolve_submit_button(page, ad)
                    if sub is not None:
                        sub.scroll_into_view_if_needed(timeout=8000)
                        time.sleep(0.5)
                        steps.append("已把提交按钮滚到视野内（未点击；%s）" % _sub_how)
                    else:
                        steps.append("没找到提交按钮（%s），请自行在页面上找" % _sub_how)
                except Exception as e:  # noqa: BLE001
                    steps.append("滚动到提交按钮失败：%s" % str(e)[:50])

                time.sleep(1.0)

                # ── 可见性：默认不碰，按参数设置 ──
                if args.visibility and args.visibility != "keep":
                    vtext = (ad.get("visibility") or {}).get(args.visibility)
                    if vtext:
                        tmpl = ad.get("visibility_selector_template") or "label:has-text('{text}')"
                        vsel = tmpl.replace("{text}", vtext)
                        # ★ 真机校准：这类单选是自定义组件，**可点的是外层 <label>，不是里面的
                        #   <span>** —— 点 span 会被 label 拦截（pointer-events），
                        #   Playwright 重试到超时。实测踩过（第一次自动发布因此发成了公开）。
                        #   这里先按 label 选择器点；不行就带 force 补一刀。
                        done = False
                        for how in ("normal", "force"):
                            try:
                                page.locator(vsel).last.click(timeout=8000,
                                                              force=(how == "force"))
                                steps.append("可见性已设为 %s（%s）" % (vtext, how))
                                done = True
                                break
                            except Exception as e:  # noqa: BLE001
                                last_err = str(e)[:60]
                        if not done:
                            steps.append("设置可见性失败：%s" % last_err)
                        time.sleep(1.0)
                    else:
                        steps.append("适配器没配 %s 的可见性" % args.visibility)

                # ── 提交前的收尾 + 遮挡硬闸（设计 §2.5 / §3 接入点 4）──
                gate = _settle_before_step(page, args, steps, "提交前", ad=ad)
                _blocked_note = ""
                try:
                    _sub_btn, _ = _resolve_submit_button(page, ad)   # ★ 与滚动那步同一套
                except Exception:        # noqa: BLE001
                    _sub_btn = None
                if _sub_btn is not None:
                    _blocked, _why = dialog_settle.is_occluded_by_layer(page, _sub_btn)
                    if _blocked:
                        _st, _blocked_note = _blocked_submit_status(name, _why, bool(args.submit))
                        steps.append(_blocked_note)
                        dialog_settle.dump_settle_failure(
                            page, {"note": _blocked_note, "url": page.url, "settle": gate},
                            _settle_dump_dir(args))
                        if args.submit:
                            w(args.status, {"state": _st, "note": _blocked_note, "steps": steps})
                            browser_state.close(ctx, args.profile)
                            return

                try:
                    page.screenshot(path=str(Path(args.status).with_suffix(".png")))
                except Exception:
                    pass

                # ── 提交：只有显式要求才点 ──
                if not args.submit:
                    # ★ 挡住了就把原因写进 note（2026-09-29 复查 finding 2）：
                    #   note 会被当成 error 显示在界面上，steps 不会 —— 用户能看到的只有它。
                    note = _blocked_note or (
                        "%s：已填好，**停在提交前**——窗口已打开、【%s】已滚到眼前，"
                        "你核对后自己点提交；不想发就直接关窗。" % (name, sub_text))
                    w(args.status, {"state": "manual", "note": note, "steps": steps,
                                    "url": page.url})
                    _hold(ctx, page, args, steps, name)
                    return

                w(args.status, {"state": "running", "note": "点【%s】提交…" % sub_text, "steps": steps})
                try:
                    btn = None
                    bsel = ad.get("submit_button")
                    if bsel:
                        cand = page.locator(bsel)
                        # 页面上「作品发布」也含"发布"字样，取最后一个（真提交按钮在底部）
                        btn = cand.last if cand.count() > 1 else cand.first
                    if btn is None or btn.count() == 0:
                        btn = page.get_by_role("button", name=sub_text, exact=True).last
                    btn.click(timeout=15000)
                    steps.append("已点【%s】" % sub_text)
                except Exception as e:  # noqa: BLE001
                    w(args.status, {"state": "fail", "note": "点提交失败：%s" % str(e)[:150], "steps": steps})
                    browser_state.close(ctx, args.profile)
                    return

                time.sleep(ad.get("after_submit_wait", 10))
                try:
                    page.screenshot(path=str(Path(args.status).with_suffix(".png")))
                except Exception:
                    pass
                txt2 = ""
                try:
                    txt2 = page.evaluate("document.body ? document.body.innerText : ''") or ""
                except Exception:
                    pass
                hints = [h for h in (ad.get("submit_success_hints") or []) if h in txt2]
                steps.append("提交后 URL=%s" % page.url)

                # ★★ 真机实测（2026-09-27）：抖音会在提交时弹**短信二次验证**——
                #    「为确保是本人操作抖音账号，请输入当前手机号 139******29 收到的短信验证码」。
                #    首次发布往往能过，紧接着第二次就触发风控。这个验证码只有账号本人能拿到，
                #    自动化永远做不了 —— 所以必须**识别出来、停住、把窗口留给用户**，
                #    而不是点完提交就以为成功了。
                vch = [h for h in (ad.get("verify_dialog_hints") or []) if h in txt2]
                if vch:
                    steps.append("检测到平台二次验证弹窗：%s" % "、".join(vch[:3]))
                    f_note = ("%s：平台要求**短信二次验证**（风控）。窗口已留着，"
                              "请在弹出的对话框里填验证码完成验证；也可以通过「使用原设备扫码」。"
                              "工具无法代填验证码。" % name)
                    w(args.status, {"state": "manual", "reason": "verify_required",
                                    "note": f_note, "steps": steps, "url": page.url})
                    _hold(ctx, page, args, steps, name, final_state="manual", final_note=f_note)
                    return
                # 提交后跳到作品管理页也是强信号（抖音实测：/content/manage?enter_from=publish）
                # 确定性判定：去作品管理页看标题在不在列表里。
                # ★ 比猜 URL 靠谱 —— 实测抖音提交后的落点 URL 不稳定
                #   （一次是 /content/manage?enter_from=publish，一次是 /content/post/video）。
                if not hints and ad.get("manage_url"):
                    try:
                        page.goto(ad["manage_url"], wait_until="domcontentloaded", timeout=45000)
                        time.sleep(6)
                        mtxt = page.evaluate("document.body ? document.body.innerText : ''") or ""
                        if args.title and args.title in mtxt:
                            hints = ["作品管理列表里已出现该作品"]
                            steps.append("到作品管理页确认：标题已在列表中 ✓")
                        else:
                            steps.append("到作品管理页未找到该标题，可能未发布成功")
                    except Exception as e:  # noqa: BLE001
                        steps.append("查作品管理页失败：%s" % str(e)[:50])

                jumped = any(k in page.url for k in ("manage", "content/list", "success"))
                if hints:
                    steps.append("成功标记：%s" % "、".join(hints[:3]))
                elif jumped:
                    steps.append("已跳到作品管理类页面（视为提交成功）")
                good = bool(hints) or jumped
                f_state = "success" if good else "manual"
                f_note = ("%s：已自动提交，%s" % (name, "页面出现成功标记（%s）" % "、".join(hints[:3])
                                                  if hints else "提交后已跳到作品管理页")
                          if good else
                          "%s：已点提交，但没读到明确成功标记，请到平台确认" % name)
                w(args.status, {"state": f_state, "note": f_note, "steps": steps, "url": page.url})
                # ★ 自动提交模式下**不再摁住窗口**（见 _hold 的说明）：活干完了就退，
                #   否则引擎要按 --hold（默认 15 分钟）干等，用户看到的是"发完了一直等待"。
                _hold(ctx, page, args, steps, name, final_state=f_state, final_note=f_note,
                      need_hold=not args.submit)
    except Exception as e:  # noqa: BLE001
        w(args.status, {"state": "fail", "note": str(e)[:200], "steps": steps})
        sys.exit(1)


def verify_published(title, ad, profile_dir, headless=True):
    """SAU 跑完后**独立核验**：去作品管理页看标题在不在列表里。返回 (state, note)。

      ("success", "已独立核验：作品管理页里能看到该标题")
      ("manual",  "没找到标题 / 核验本身没做成 —— **都归 manual**（请人工核对），不谎报成功")
      ("skip",    "")        ← 没配 manage_url / 没标题：**没法核验**，不动状态、不加文案

    ★ 为什么必须有（2026-09-29 真机，用户现场核对发现）：上游抖音上传器的"成功"判据只有
      一句"URL 跳没跳到作品管理页" —— 实测 11:18:23 点发布、**11:18:24 就宣布成功**，
      而页面上提示"服务器问题"、创作中心里**也没有新视频**。⇒ 上传器自报的成功**不能信**。
      这里用**独立的一步**（另开浏览器、在管理页里找标题文本）来核 ——
      与 adapter 路径给抖音用的那套办法（manage_url + 标题在不在列表里）一致。
    """
    url = str((ad or {}).get("manage_url") or "")
    if not url or not title:
        return "skip", ""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:                # noqa: BLE001
        return "manual", "独立核验没做成（未安装 playwright：%s）—— 请人工核对" % str(e)[:40]
    try:
        with sync_playwright() as p:
            with browser_state.persistent_context(p.chromium,
                user_data_dir=str(profile_dir), channel=browser_channel.channel(),
                headless=bool(headless), viewport={"width": 1280, "height": 820}) as ctx:
                try:
                    pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                    pg.goto(url, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(6)                 # 列表是前端渲染的，给点时间
                    txt = pg.evaluate("document.body ? document.body.innerText : ''") or ""
                finally:
                    try:
                        browser_state.close(ctx, profile_dir)
                    except Exception:            # noqa: BLE001
                        pass
        if title in txt:
            return "success", "已独立核验：作品管理页里能看到该标题"
        # ★ 文案不要写 **（2026-09-29 OCR 复查指出）：界面是 esc() 纯文本渲染，
        #   `**` 会原样显示；本项目早就为此把 notes 里的星号去掉了。
        return "manual", ("上传器报成功，但作品管理页里没找到该标题"
                          "（可能还在审核，或根本没发出去）—— 请人工核对")
    except Exception as e:                # noqa: BLE001
        return "manual", "独立核验没做成（%s）—— 请人工核对" % str(e)[:60]


def run_via_sau(args, ad, name):
    """用 social-auto-upload 的上传器发布（6 个平台走这条）。

    ★ 为什么需要单独的进度转发：SAU 是「一口气跑完」的函数，中途不回调我们。
      所以起一个线程盯着它的日志文件，把每行转成我们的状态文件 ——
      这样 UI 能看到实时进度，**尤其是「平台要求短信验证码」那一条**：
      那时用户要做的就是在弹出的浏览器窗口里直接输验证码（有头模式才看得到）。
    """
    data_dir = Path(args.status).parent.parent      # <data>/publish/job_N.json → <data>
    log_path = Path(args.status).with_suffix(".sau.log")
    if log_path.exists():
        try:
            log_path.unlink()
        except OSError:
            pass

    verify_file = data_dir / "sau" / "verify_code.txt"
    base_note = "%s：正在用 social-auto-upload 上传器发布…" % name
    w(args.status, {"state": "running", "note": base_note})

    stop = threading.Event()

    def tail():
        seen = 0
        while not stop.is_set():
            try:
                if log_path.exists():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    if len(lines) > seen:
                        for ln in lines[seen:]:
                            msg = ln.split("] ", 1)[-1].strip() if "] " in ln else ln.strip()
                            if not msg:
                                continue
                            note = msg
                            if "验证码" in msg:
                                # ★ 两处改进（2026-09-28 用户实测反馈）：
                                #   ① 去掉 **星号** —— 这是纯文本提示，星号会原样显示出来
                                #      （OCR 也在设置页抓到过同一类问题）；
                                #   ② **说清楚窗口在哪**：原来只写「在弹出的浏览器窗口里」，
                                #      用户在自己电脑上找了一圈没找到 —— 那台浏览器其实跑在
                                #      服务器的虚拟屏幕上，得开 noVNC 才看得到。
                                note = ("%s：平台要求短信验证码。" % name
                                        + "请打开 http://<这台NAS的IP>:6080/vnc.html （口令见部署说明）"
                                        + "在里面的浏览器窗口直接输入；"
                                        + "也可把验证码写进 %s" % verify_file)
                            w(args.status, {"state": "running", "note": note[:200]})
                        seen = len(lines)
            except OSError:
                pass
            time.sleep(1.5)

    t = threading.Thread(target=tail, daemon=True)
    t.start()
    try:
        r = sau_bridge.publish(
            args.key,
            video=args.video,
            title=args.title or "",
            body=args.body or "",
            topics=args.topics or "",
            profile_dir=args.profile,
            data_dir=data_dir,
            headless=bool(args.headless),
            timeout=max(120, args.hold),
            log_file=str(log_path),
            thumbnail=args.thumbnail or None,
        )
    finally:
        stop.set()
        t.join(timeout=3)

    steps = ["引擎=social-auto-upload", "日志=%s" % log_path.name]
    if r.get("ok"):
        base = r.get("note") or ("%s：已发布" % name)
        # ★ 独立核验（2026-09-29）：上传器说成功 ≠ 真发出去了 —— 自己再去管理页看一眼。
        try:
            # ★ 直接用调用方传进来的 ad（OCR 复查指出：重读 load_adapter 是多余的磁盘 IO，
            #   而且可能和发布实际用的那份不一致）
            v_state, v_note = verify_published(args.title, ad, args.profile,
                                               headless=bool(args.headless))
        except Exception as e:            # noqa: BLE001
            v_state, v_note = "manual", "独立核验异常（%s）—— 请人工核对" % str(e)[:60]
        if v_state == "skip":             # 没配管理页 ⇒ 没法核验：别改状态、别加噪音
            w(args.status, {"state": "success", "note": base, "steps": steps})
            return 0
        if v_note:
            steps.append(v_note)
        w(args.status, {"state": v_state, "note": "%s：%s" % (name, v_note), "steps": steps})
        return 0
    w(args.status, {"state": "fail", "note": r.get("error") or ("%s：发布失败" % name),
                    "reason": "sau_failed", "steps": steps})
    return 1


def _hold(ctx, page, args, steps, name, final_state="manual", final_note="", need_hold=True):
    """保持窗口打开等人工处理；窗口被关掉或超时后按 final_state 收尾。

    ★ final_state 必须由调用方显式给：自动提交成功时是 success。
      旧实现一律写 manual —— 于是「已发布成功」会被收尾这一步降级成「需人工确认」，
      而且文案还会写「不点提交」，与事实相反。实测踩过。

    ★★ 2026-09-30（用户报「头条/B站发完发布台一直等待」）：**自动提交模式下活已经干完了**，
      这里却仍按 `--hold`（默认 **900 秒＝15 分钟**）把窗口摁住 —— 引擎只能干等，
      界面上就是"发布成功却一直在跑"，用户手动中止才收尾（job #74/#75 的日志里写着
      「被停时 worker 已给出终态」）。所以加了 need_hold：
        · 停手模式（不带 --submit）→ 照旧 hold，等人来点提交 ✓
        · 二次验证要人填码 / 找不到上传入口 → 由那两处调用点照旧 hold ✓
        · **自动提交且已给出终态 → need_hold=False：写完状态立刻关窗退出** ✓
    """
    # ★ 停住的第一件事：把现场落盘（标定选择器用）。
    #   头条/B站的封面入口**要上传视频之后才出现**，不上传就没法标定；
    #   而这里正是"视频已上传、表单已填好"的那一刻 —— 标定所需的现场就在眼前。
    #   落盘的是「结构摘要 + 整页 HTML」，见 dump_hold_snapshot 的说明。
    try:
        dump_hold_snapshot(page, args.status)
    except Exception:  # noqa: BLE001
        pass

    if need_hold:
        deadline = time.time() + max(30, args.hold)
        while time.time() < deadline:
            time.sleep(3)
            try:
                if not page.url:
                    break
            except Exception:
                break            # 页面已关闭
    if not final_note:
        final_note = ("%s：窗口已关闭；工具只做填写、**不点提交**，"
                      "是否发布成功请到平台确认" % name)
    try:
        w(args.status, {"state": final_state, "note": final_note,
                        "steps": steps + ["窗口关闭"]})
    except Exception:
        pass
    try:
        browser_state.close(ctx, args.profile)
    except Exception:
        pass


if __name__ == "__main__":
    main()

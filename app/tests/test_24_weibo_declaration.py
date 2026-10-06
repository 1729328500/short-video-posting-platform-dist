# -*- coding: utf-8 -*-
"""自测：微博「内容声明」这步的选择器**必须是实测过的文案**

运行：python tests/test_24_weibo_declaration.py

★ 背景（2026-09-29 实测，job_32）：微博发布**卡死在「内容声明」**这一步 ——
   弹层明明开着（截图里 5 个选项清清楚楚），代码却找不到「含AI生成内容」，
   8 秒超时、发布停住。现场落盘文件（data/sau/weibo_decl_failure.json/.png）
   给出的答案是：**微博把文案改成了「内容由AI生成」**，旧文案在页面上不存在。

  这次能一次标定成功，靠的是决策 3.1「够不到的步骤让程序把现场落盘」——
  那一步的 DOM 只有视频传完才存在，人工和探针都够不到。

判据（都是**从源码/现场文件就能验的**，不测浏览器）：
  ① 候选文案里**必须**有实测文案「内容由AI生成」，且排在最前；
  ② 旧文案「含AI生成内容」保留为兜底（文案可能按灰度/地区不同）；
  ③ 触发下拉仍以**语义类** `div.wbpro-form span.woo-pop-ctrl` 优先 ——
     哈希类名是定时炸弹（微博一发版就变，job_27 就是这么死的）；
  ④ 找不到选项时**先落盘再抛**（决策 3.1 / 3.4：失败必须给证据，不能只是说明）。

★ 为什么是源码断言而不是真开浏览器：本地 venv 没有 playwright（那套只在容器里跑），
  而这仓库的既有约定就是**测接线、选择器靠落盘人工标定**（见 test_23 的判据 ①~⑤）。
  所以这里锁的是「实测结论别被后来的改动悄悄丢掉」，不是替代真机运行。
"""
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
SRC = APP / "vendor" / "sau" / "uploader" / "weibo_uploader" / "main.py"

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def _method_body(text, name):
    """抽出某个方法的**可执行代码**（到下一个方法为止），去掉 docstring 与整行注释。

    ★ 为什么非去掉不可（写这条测试时踩的）：`_select_declaration` 的 docstring
      和注释里**都会引用**哈希类名（讲它当年是怎么死的）。若连注释一起断言，
      「③ 语义类排在哈希类之前」就会因为**注释里先提到哈希**而 FAIL ——
      判据自己说谎，比没有判据更坏。所以只对**代码行**下判据。
    另外这样断言也不会被**别处**同名文本误命中（常量定义了、用的时候却还硬编码，
    正是「看着改了其实没生效」）。
    """
    chunks = text.split("\n    async def ")
    for i, c in enumerate(chunks):
        if c.startswith(name):
            body = "\n    async def ".join(chunks[i:])
            m = re.search(r'""".*?"""', body, re.S)
            if m:
                body = body[:m.start()] + body[m.end():]
            body = "\n".join(ln for ln in body.split("\n")
                             if not ln.strip().startswith("#"))
            return body
    return ""


def main():
    if not SRC.exists():
        ok("源文件存在", False, str(SRC))
        return finish()
    text = SRC.read_text(encoding="utf-8")

    # ①② 候选文案：新文案在前，旧文案兜底
    m = re.search(r"DECL_OPTION_TEXTS\s*=\s*\(([^)]*)\)", text)
    ok("① 定义了候选文案常量 DECL_OPTION_TEXTS", bool(m))
    cands = m.group(1) if m else ""
    ok("① 含有实测文案「内容由AI生成」", "内容由AI生成" in cands, cands or "(没找到常量)")
    ok("② 旧文案「含AI生成内容」仍作兜底", "含AI生成内容" in cands, cands or "(没找到常量)")
    if "内容由AI生成" in cands and "含AI生成内容" in cands:
        ok("① 新文案排在旧文案前面",
           cands.index("内容由AI生成") < cands.index("含AI生成内容"), cands)

    sel = _method_body(text, "_select_declaration")
    ok("① _select_declaration 存在", bool(sel))
    # 常量必须**真的被接线**：只定义不用 = 死代码，等于没修
    ok("① 选择逻辑真的用了这个常量（不是只定义没人用）",
       "DECL_OPTION_TEXTS" in sel)

    # ③ 触发下拉：语义类优先（哈希类名只留兜底）
    i_sem = sel.find("div.wbpro-form span.woo-pop-ctrl")
    i_hash = sel.find("_gap1_nsgmr")
    ok("③ 用语义类定位触发下拉", i_sem >= 0)
    ok("③ 语义类排在哈希类之前（哈希只作兜底）",
       i_sem >= 0 and (i_hash < 0 or i_sem < i_hash))

    # ④ 失败必须给证据：先落盘再抛
    i_dump = sel.find("_dump_declaration_failure(page)")
    i_raise = sel.find("raise", i_dump) if i_dump >= 0 else -1
    ok("④ 找不到选项时先落盘再抛", i_dump >= 0 and i_raise > i_dump,
       "dump@%d raise@%d" % (i_dump, i_raise))

    dump = _method_body(text, "_dump_declaration_failure")
    ok("④ 落盘函数存在", bool(dump))
    ok("④ 落盘位置取自 SAU 的 BASE_DIR（换数据卷也找得到）", "BASE_DIR" in dump)

    return finish()


def finish():
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

# -*- coding: utf-8 -*-
"""自测：话题/短标题里的「符号不正确」（2026-09-28 用户实测发现）

运行：python tests/test_21_topics_short_title.py

★ 背景：视频号发布时「发表」按钮**一直是灰的、点不动**，重试 5 分钟最终失败
  （job_12）。用户在 noVNC 里亲眼看到表单上有个不合法的符号。查下来是**三处**
  同一类毛病 —— 都是「处理用户/AI 给的文本时，符号没清干净」：

  **A. 视频号短标题的补足词自带非法字符**（vendored，`tencent_uploader/main.py`）
     上游 `format_str_for_short_title()` 的过滤器**不允许全角逗号**（它只把半角 ","
     换成空格，其余非字母数字一律丢），可它补足长度用的偏偏是 `"，精彩内容分享"` ——
     开头就是全角逗号；而且补足发生在过滤**之后**，等于绕过了自己的检查。
     标题不足 7 字时必然踩中：`「测试」→「测试，精彩内容」`。

  **B. `sau_bridge.parse_topics()` 只剥开头的 `#`**
     `lstrip("#")` 去不掉尾巴那个。微博格式是 `#词#`（首尾都有），于是：
       `'#旧房翻新# #墙面翻新#'` → `['旧房翻新#', '墙面翻新#']`  ← 标签里留了 #
       `'#旧房翻新#墙面翻新'`    → `['旧房翻新#墙面翻新']`       ← 两个词粘成一个

  **C. 前端 `topicsFor()` 同样不把 `#` 当分隔符，却给每个词再补一遍 `#`**
     输入里本来就有 `#` 时就叠加成双井号：
       `'#旧房翻新# #墙面翻新#'` --微博--> `'##旧房翻新## ##墙面翻新##'`

判据：
  ① 短标题：任何输入下都**不含**过滤器不认的字符，且长度落在 7~15；
  ② 短标题：标题够长时不被改动（别把好的改坏）；
  ③ ④ ⑤ parse_topics：微博格式、连写、纯词三种输入都要拆干净；
  ⑥ ⑦ 前端 topicsFor：微博不再出双井号，其它平台也不带多余符号。
"""
import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))

from sau_bridge import parse_topics  # noqa: E402

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


# ---------- A：从 vendored 文件里把真函数抽出来跑（本地装不了 patchright，导不进来）----------
def load_short_title_fn():
    src = (APP / "vendor" / "sau" / "uploader" / "tencent_uploader" / "main.py").read_text(
        encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "format_str_for_short_title":
            seg = ast.get_source_segment(src, node)
            ns = {}
            exec(seg, ns)                    # noqa: S102 —— 测的就是这段真代码
            return ns["format_str_for_short_title"]
    return None


ALLOWED = "《》“”:+?%°"


def illegal_chars(s):
    # 空格算合法：函数本来就**故意**把半角逗号换成空格（注释里只担心「尾部空格
    # 会被平台 trim 掉导致长度不达标」），不是「不允许空格」。
    return [c for c in s if not (c.isalnum() or c in ALLOWED or c == " ")]


def test_short_title():
    fmt = load_short_title_fn()
    ok("A 从 vendored 文件里取到了 format_str_for_short_title", fmt is not None)
    if fmt is None:
        return

    cases = ["测试", "测试标题", "短", "a", "这是一个足够长的测试标题", "测试，带逗号",
             "abc,def", "标题!@#$%^&*()", ""]
    bad = []
    for c in cases:
        r = fmt(c)
        prob = []
        if illegal_chars(r):
            prob.append("非法字符 %s" % illegal_chars(r))
        if not (7 <= len(r) <= 15):
            prob.append("长度 %d 不在 7~15" % len(r))
        if prob:
            bad.append("%r -> %r (%s)" % (c, r, "; ".join(prob)))
    ok("① 所有输入的短标题都合法且在 7~15 字内", not bad, " || ".join(bad[:3]))

    # ② 标题够长时不该被改动
    long_title = "这是一个足够长的测试标题"
    ok("② 够长的标题原样返回", fmt(long_title) == long_title, fmt(long_title))
    ok("② 恰好 7 字的标题不被补足", fmt("七个字的标题啊") == "七个字的标题啊",
       fmt("七个字的标题啊"))
    ok("② 15 字标题不被截断", len(fmt("一二三四五六七八九十十一十二十三十四十五")) == 15,
       fmt("一二三四五六七八九十十一十二十三十四十五"))


# ---------- B：后端话题解析 ----------
def test_parse_topics():
    cases = [
        ("#旧房翻新 #墙面翻新", ["旧房翻新", "墙面翻新"], "常见格式"),
        ("#旧房翻新# #墙面翻新#", ["旧房翻新", "墙面翻新"], "★ 微博格式（首尾都有 #）"),
        ("#旧房翻新#墙面翻新", ["旧房翻新", "墙面翻新"], "★ 连写无分隔"),
        ("旧房翻新 墙面翻新", ["旧房翻新", "墙面翻新"], "不带 #"),
        ("#测试, #自动化", ["测试", "自动化"], "逗号分隔"),
        ("", [], "空"),
    ]
    fails = []
    for raw, want, desc in cases:
        got = parse_topics(raw)
        if got != want:
            fails.append("%s: %r -> %r（期望 %r）" % (desc, raw, got, want))
    ok("③④⑤ parse_topics 六种输入全部分解正确", not fails, " || ".join(fails[:3]))
    ok("⑤ 结果里任何词都不含 #",
       all("#" not in t for t in parse_topics("#a# #b# #c#")),
       parse_topics("#a# #b# #c#"))


# ---------- C：前端 topicsFor（用 node 跑真函数）----------
def load_topics_for():
    src = (APP / "ui" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"function topicsFor\(k, s\) \{.*?\n  \}", src, re.S)
    return m.group(0) if m else None


def test_frontend():
    node = shutil.which("node")
    if not node:
        ok("C 有 node 可以跑前端函数", False, "找不到 node")
        return
    fn = load_topics_for()
    ok("C 从 app.js 里取到了 topicsFor", fn is not None)
    if not fn:
        return

    script = fn + """
const cases = [
  ['weibo', '#旧房翻新# #墙面翻新#'],
  ['douyin', '#旧房翻新 #墙面翻新'],
  ['weibo', '旧房翻新 墙面翻新'],
  ['douyin', '#旧房翻新#墙面翻新'],
];
console.log(JSON.stringify(cases.map(([k, s]) => [k, s, topicsFor(k, s)])));
"""
    p = Path(APP / "tests" / "_tmp21")
    p.mkdir(parents=True, exist_ok=True)
    f = p / "t.js"
    f.write_text(script, encoding="utf-8")
    out = subprocess.run([node, str(f)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if out.returncode != 0:
        ok("C node 跑通了", False, (out.stderr or "")[:200])
        return
    data = json.loads(out.stdout.strip().splitlines()[-1])

    if any("##" in got for _, _, got in data):
        ok("⑥⑦ 前端规整后不出现双井号", False,
           str([d for d in data if "##" in d[2]][:2]))

    # 逐条比标准输出（微博是 #词#、其余是 #词；输入带不带 # 都要规整成一样）
    want = {
        ("weibo", "#旧房翻新# #墙面翻新#"): "#旧房翻新# #墙面翻新#",
        ("weibo", "旧房翻新 墙面翻新"): "#旧房翻新# #墙面翻新#",
        ("douyin", "#旧房翻新 #墙面翻新"): "#旧房翻新 #墙面翻新",
        ("douyin", "#旧房翻新#墙面翻新"): "#旧房翻新 #墙面翻新",
    }
    bad = []
    for k, src, got in data:
        exp = want.get((k, src))
        if exp is not None and got != exp:
            bad.append("%s %r -> %r（期望 %r）" % (k, src, got, exp))
    ok("⑥⑦ 前端输出逐条符合预期（微博 #词#／其余 #词）", not bad, " || ".join(bad[:3]))
    ok("⑥ 微博不再出双井号", not any("##" in got for k, _, got in data if k == "weibo"),
       str([d for d in data if d[0] == "weibo"]))


def main():
    test_short_title()
    test_parse_topics()
    test_frontend()

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

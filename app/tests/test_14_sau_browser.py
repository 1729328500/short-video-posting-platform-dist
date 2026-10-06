# -*- coding: utf-8 -*-
"""自测：vendored 的 SAU 上传器必须使用**镜像里真的装了的**浏览器

运行：python tests/test_14_sau_browser.py

★ 为什么需要这个测试（2026-09-28 实测踩过）：
  视频号那个上传器 `uploader/tencent_uploader/main.py` 把 channel 写死成 `"chrome"`
  —— 也就是要求系统里装 Google Chrome。而镜像里只有 playwright / patchright
  自带的 chromium，没有 Google Chrome。抖音/快手/小红书/微博那几个都写的是
  `"chromium"`，**只有视频号不一样**。后果是：

      抖音能发、视频号一提交就炸：
      BrowserType.launch: Chromium distribution 'chrome' is not found at
      /opt/google/chrome/chrome

  更麻烦的是：`NOTICE.md` 和 `Dockerfile` 的注释当时都写着
  「SAU 写死 channel="chromium"」—— **与实际不符**，所以这个坑一直没人发现。

  这个测试把「所有上传器都只用自带的 chromium」变成一条可回归的不变量：
  以后新增上传器、或升级上游覆盖同名文件时，会立刻在这里报警，
  而不是等到线上发布失败才发现。

判据（两条）：
  ① `vendor/sau` 下任何位置都不得出现 `channel="chrome"`（对应 chromium 的写法应为其本身）；
  ② 需要外部浏览器二进制时，只能通过 `LOCAL_CHROME_PATH` 走，且其默认值必须为空
     （留空 = 用 patchright 自带的 chromium）。
"""
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
VENDOR = APP / "vendor" / "sau"

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


# 两种写法都要认：`channel = "chrome"` 与 `launch_kwargs["channel"] = "chrome"`。
# 注意 "chromium" 的收尾引号不同，不会被误伤。
BAD_CHANNEL = re.compile(r"""(?:\[["']channel["']\]|\bchannel)\s*[=:]\s*["']chrome["']""")
# 允许的写法：chromium / 走 LOCAL_CHROME_PATH
GOOD_CHANNEL = re.compile(r"""(?:\[["']channel["']\]|\bchannel)\s*[=:]\s*["']chromium["']""")


def scan_py():
    """遍历 vendor/sau 下的 .py，返回 [(相对路径, 行号, 行内容)]"""
    hits = []
    for p in sorted(VENDOR.rglob("*.py")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            hits.append((p.relative_to(VENDOR).as_posix(), i, line))
    return hits


def main():
    if not VENDOR.exists():
        print("找不到 vendor 目录：%s" % VENDOR)
        return 1

    files = sorted(VENDOR.rglob("*.py"))
    ok("vendor/sau 下有 Python 文件", len(files) > 0, "%d 个" % len(files))

    lines = scan_py()

    # ① 不得有 channel="chrome"
    bad = [(f, i, ln.strip()) for f, i, ln in lines if BAD_CHANNEL.search(ln)]
    detail = "；".join("%s:%d  %s" % (f, i, ln) for f, i, ln in bad[:5])
    ok("没有任何上传器申请 Google Chrome（channel=\"chrome\"）", not bad, detail)

    # ② 视频号那个上传器必须显式用 chromium
    tencent = VENDOR / "uploader" / "tencent_uploader" / "main.py"
    if tencent.exists():
        body = tencent.read_text(encoding="utf-8", errors="replace")
        fn = body.find("def _build_launch_kwargs")
        # 取到下一个顶格 def 为止（不按固定字数切——注释一改长度就切歪）
        nxt = body.find("\ndef ", fn + 1) if fn >= 0 else -1
        seg = body[fn:nxt] if fn >= 0 and nxt > fn else (body[fn:] if fn >= 0 else "")
        ok("视频号 _build_launch_kwargs 用 chromium",
           bool(GOOD_CHANNEL.search(seg)),
           seg.strip().replace("\n", " ")[:160])
    else:
        ok("视频号上传器存在", False, str(tencent))

    # ③ LOCAL_CHROME_PATH 默认必须为空
    conf = VENDOR / "conf.py"
    if conf.exists():
        m = re.search(r"""^LOCAL_CHROME_PATH\s*=\s*(.+)$""",
                      conf.read_text(encoding="utf-8", errors="replace"), re.M)
        raw = m.group(1) if m else "<未找到>"
        val = raw.split("#")[0].strip()          # 去掉行尾注释再判
        ok("LOCAL_CHROME_PATH 默认留空（= 用自带 chromium）",
           val in ('""', "''"), val)
    else:
        ok("conf.py 存在", False, str(conf))

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

# -*- coding: utf-8 -*-
"""OCR 代码审查包装 —— 让 app/tests/ 也进审查范围。

★ 为什么需要这个包装，而不是直接跑 `ocr review`：

  open-code-review 按内置规则 `default_path` 排除测试文件，
  而且**没有开关能打开**（`--exclude` 只能排除更多 —— 实测过）。
  本次安全加固的判据有一大半住在测试里（跨站来源被拒、损坏配置不放行、
  旧会话失效、慢上传不持锁……），漏审等于没审。

  ★ 排除判断看的是【文件名】不是路径（居帮帮 2026-09-22 逐个试探过）：
      排除：test_probe.py · produce_test.py · produce_spec_test.py
      放行：TestProbe.py（大小写敏感）· tests_thing.py · mytest.py · produce_spec.py

  变通：把**改过的**测试文件复制到仓库内 `.ocr-review/`，并改名为
  `ocr_<原名去掉 test_ 前缀>.py`，ocr 就会把它当新增文件审。
  映射关系会拼进 --background 告诉审阅者。
  （不镜像全部测试 —— 那会把几十个文件都卷进来，白烧 token。）

用法：
    cd <项目根> && python -X utf8 tools/ocr-review.py -b "本次改了什么" [-o 输出.md] [--timeout 8]

★★ 两条踩过的坑，改这个脚本前务必先读：

  1. **`.ocr-review/` 绝对不能加进 .gitignore。**
     ocr 是 git diff 驱动的：一进 gitignore，git 就看不见镜像 →
     ocr 自然也看不见 →「测试也审了」变成空话，
     **而脚本照样打印"镜像了 N 个文件，已清理镜像目录"**，一切看起来正常。
     （本项目 .gitignore 目前没有这条 —— 加之前先想清楚。）

  2. **不要用 shell=True，也不要加 `cmd.exe /c`。**
     shell=True：Windows 会把参数重组成命令行交给 cmd.exe 重解析，
     `-b` 里的 `&` `|` `<` `>` 会被当成命令分隔符。
     `cmd.exe /c`：实测会把 `--output` 直接吞掉（结果文件一直写不出来）。
     `shutil.which("ocr")` 返回的是 `ocr.CMD`，subprocess 能直接执行它。

跑完自动清理镜像。万一脚本崩了，镜像以【未跟踪文件】出现在 git status 里 ——
看得见，比被静默忽略强。提交时用显式路径，不会误扫进去。
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIRROR = REPO / ".ocr-review"
WATCH = "app/tests"               # 被默认规则排除、又确实需要审的目录


def changed_test_files(from_ref=None, to_ref=None) -> list[Path]:
    """有改动的测试文件。

    from_ref/to_ref 给定 → 走「范围模式」（已提交的整批改动）；
    否则走「工作区模式」（未提交的 staged+unstaged+untracked）。

    ★★ 两个坑，都会让文件【静默不被审】—— 而这个脚本表面上照常打印"镜像了 N 个文件"：

      1. `git status --porcelain` 默认（core.quotePath=true）会转义非 ASCII 路径，
         输出成 `"app/tests/\\346\\265\\213\\350\\257\\225.py"` ——
         带引号和八进制转义，`Path.suffix` 就不再是 .py，文件被跳过。
         本项目有中文路径，这条路必然踩。
      2. 不带 `-uall` 时，git 会把一个【全新的未跟踪目录】折叠成一行 `?? dir/`，
         里面的测试文件全部被跳过。

    ⇒ 用 `-z`：NUL 分隔，**完全不做引号与转义**，两条一次解决。
    """
    if from_ref or to_ref:
        # 范围模式：`git diff --name-only -z` 只输出路径，**没有** XY 前缀
        rng = "%s..%s" % (from_ref or "HEAD", to_ref or "HEAD")
        raw = subprocess.run(
            ["git", "-c", "core.quotePath=false", "diff", "--name-only", "-z", rng,
             "--", WATCH],
            cwd=str(REPO), capture_output=True, text=True,
            encoding="utf-8", check=True).stdout
        return [REPO / p for p in raw.split("\0")
                if p.endswith(".py") and (REPO / p).is_file()]

    # 工作区模式：`git status --porcelain -z` 每项是 "XY <path>"
    raw = subprocess.run(
        ["git", "-c", "core.quotePath=false", "status", "--porcelain", "-z", "-uall",
         "--", WATCH],
        cwd=str(REPO), capture_output=True, text=True,
        encoding="utf-8", check=True).stdout

    files: list[Path] = []
    entries = raw.split("\0")
    i = 0
    while i < len(entries):
        e = entries[i]
        i += 1
        if len(e) < 4:
            continue                      # 末尾空串
        xy, path = e[:2], e[3:]
        if "R" in xy or "C" in xy:
            i += 1                        # rename/copy 后面还跟一个原路径字段
        p = REPO / path
        if path.endswith(".py") and p.is_file():
            files.append(p)
    return files


def mirror_name(src: Path) -> str:
    """镜像文件名：必须【不匹配】ocr 的 default_path 规则。

    ★ ocr 排除的是【文件名】不是路径 —— 以 `test_` 开头、或以 `_test.py` 结尾的会被排除。
      所以要改成 `ocr_<原文件名去掉 test_ 前缀>.py`。
      本项目的测试是 `test_31_security_hardening.py` → `ocr_31_security_hardening.py`。
    """
    stem = src.stem
    if stem.startswith("test_"):
        stem = stem[len("test_"):]
    elif stem.endswith("_test"):
        stem = stem[: -len("_test")]
    return f"ocr_{stem}.py"


def mirror_commit(to_ref=None) -> str:
    """把镜像文件做成一个**临时提交对象**，让范围模式能看见它们。

    ★★ 为什么必须这样（2026-10-06，范围模式第一次跑完被 OCR 自己抓出来）：
      范围模式 `--from X --to Y` 是**提交区间**驱动的，区间里没有未跟踪文件 ——
      镜像建了也白建，`--preview` 的 "Will review" 列表里根本没有它们，
      而本脚本照样打印"镜像了 N 个文件"。这正是模块文档警告的那种静默不审，
      只是换了个触发条件（工作区模式没这问题：它包含 staged/unstaged/untracked）。

      用 `git commit-tree` 造提交对象：**不动分支、不动工作树、不改 HEAD**，
      只在对象库里多一个悬空提交，用完即弃（gc 会回收）。
      索引会被临时改一下（add 再 reset），跑完立刻还原。
    """
    mirror_rel = str(MIRROR.relative_to(REPO))
    subprocess.run(["git", "add", "--", mirror_rel], cwd=str(REPO), check=True)
    try:
        tree = subprocess.run(["git", "write-tree"], cwd=str(REPO),
                              capture_output=True, text=True, check=True).stdout.strip()
        head = subprocess.run(["git", "rev-parse", to_ref or "HEAD"], cwd=str(REPO),
                              capture_output=True, text=True, check=True).stdout.strip()
        commit = subprocess.run(
            ["git", "commit-tree", tree, "-p", head, "-m", "ocr-review mirrors (temporary)"],
            cwd=str(REPO), capture_output=True, text=True, check=True).stdout.strip()
        return commit
    finally:
        # 还原索引：不能让调用方的工作区/暂存区因为我们而变形
        subprocess.run(["git", "reset", "-q", "--", mirror_rel], cwd=str(REPO))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-b", "--background", required=True,
                    help="本次改动的业务背景（会显著影响审查质量）")
    ap.add_argument("-o", "--output", default=None, help="结果写到这个文件")
    ap.add_argument("--timeout", default="8")
    ap.add_argument("--from", dest="from_ref", default=None,
                    help="范围审查的起点（如 f33c0b2）。给了就走已提交的整批改动，而不是工作区")
    ap.add_argument("--to", dest="to_ref", default=None, help="范围审查的终点，默认 HEAD")
    ap.add_argument("--preview", action="store_true",
                    help="只列出会被审的文件就退出 —— 用来确认镜像真的被 ocr 看见了")
    args = ap.parse_args()

    # ── 1) 镜像改过的测试文件（改名的理由见 mirror_name）──
    targets = changed_test_files(args.from_ref, args.to_ref)
    mapping: list[str] = []
    used: dict[str, Path] = {}
    for src in targets:
        name = mirror_name(src)
        # ★ 防重名：mirror_name 只在【单个目录】内保证不撞，
        #   而所有镜像都写进同一个扁平目录。撞了会静默覆盖 ——
        #   于是有一个文件从没被审过，而 mapping 里还印着两行。
        if name in used:
            raise SystemExit(
                f"镜像重名：{src} 与 {used[name]} 都映射到 {name} —— "
                f"会让其中一个【静默不审】。请改 mirror_name 的命名规则。")
        used[name] = src
        dst = MIRROR / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        mapping.append(f"  {dst.relative_to(REPO)}  ==  {src.relative_to(REPO)}")
    if mapping:
        print(f"镜像 {len(mapping)} 个测试文件 → {MIRROR.name}/")
        for m in mapping:
            print(m)
    else:
        print("没有改动的测试文件，只审生产代码")

    # ★ 把映射告诉审阅者 —— 镜像文件名被改过（见 mirror_name），
    #   不说清楚的话评审意见会指向一个不存在的路径。
    background = args.background
    if mapping:
        background += ("\n\n注意：以下文件是【测试文件的镜像副本】，"
                       "文件名为了绕开 ocr 的默认排除规则而被改写，"
                       "内容与被映射的真文件逐字节一致。评审时请按真文件路径给意见：\n"
                       + "\n".join(mapping))

    # ── 2) 跑 ocr（工作区模式：同时覆盖生产改动与镜像过来的测试）──
    ocr = shutil.which("ocr") or "ocr"
    cmd = [ocr, "review", "--audience", "agent", "--timeout", args.timeout,
           "-b", background]
    if args.from_ref or args.to_ref:
        # ★ 范围模式必须把镜像包进区间，否则测试静默不被审（见 mirror_commit）
        to = args.to_ref or "HEAD"
        if mapping:
            to = mirror_commit(to)
            print(f"镜像已包进临时提交 {to[:8]}（不动分支/工作树）")
        cmd += ["--from", args.from_ref or "HEAD", "--to", to]
    if args.preview:
        cmd += ["--preview"]

    try:
        # ★ 不加 shell=True，也不加 `cmd.exe /c` —— 两条都踩过，理由见模块 docstring。
        # ★ 也不用 ocr 的 --output：自己接 stdout 写文件。
        #   这样既绕开命令行引号问题，又避免调用方用 `| tail` 把结果截断
        #   （截断会让"8 条发现"看起来像"1 条"，踩过两次）。
        proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        out = proc.stdout or ""
        if proc.stderr and proc.stderr.strip():
            out += "\n[stderr]\n" + proc.stderr
        print(out)
        if args.output:
            dest = Path(args.output)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(out, encoding="utf-8")
            print(f"★ 完整结果已写入 {dest}")
        return proc.returncode
    finally:
        # ── 3) 清理（★ 不能靠 gitignore —— 那会让 ocr 看不见镜像，见 docstring）──
        shutil.rmtree(MIRROR, ignore_errors=True)
        print("已清理镜像目录")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

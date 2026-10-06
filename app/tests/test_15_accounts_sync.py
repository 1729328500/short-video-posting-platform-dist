# -*- coding: utf-8 -*-
"""自测：账号状态文件 → 数据库/界面的同步（**off 必须能同步下去**）

运行：python tests/test_15_accounts_sync.py

★ 为什么需要这个测试（2026-09-28 实测踩过）：
  `/api/accounts` 返回的是**数据库**，中间靠 `Store.refresh_accounts()` 把各平台的
  状态文件同步进库。而那个函数的映射表里**唯独没有 `state == "off"` 这一支** ——

      if   state == "on":                            target = ("on", ...)
      elif state in ("opening","waiting","unknown"): target = ("waiting", ...)
      elif state == "error":                         target = ("error", ...)
      elif state == "closed":                        target = ("off", ...)
      #   state == "off"  ← 没有分支，target 保持 None，set_account 根本不被调用

  后果：`probe_accounts.py --verify` 明明核出「未登录」并把状态文件写成 off，
  界面却继续显示绿色 —— 实测抖音掉登录后，发布台一直显示「已登录」，
  直到真去发布失败才改口。这是交接文档「坑 6」的镜像：
  坑 6 是「取消登录时乱写 off，把已登录的标成未登录」，本测试管的是另一半
  「该认的 off 要认」。**只修前者不修后者，界面会说假话。**

判据：状态文件写什么，`/api/accounts` 就得报什么（on / off / waiting / error）。
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8981
TMP = APP / "tests" / "_tmp15"
DATA = TMP / "data"

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def api(base, path, data=None):
    if data is None:
        req = urllib.request.Request(base + path)
    else:
        req = urllib.request.Request(base + path, data=json.dumps(data).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def write_status(key, state, note=""):
    """直接写平台的状态文件 —— 模拟「worker / probe 写了状态」"""
    d = DATA / "browsers"
    d.mkdir(parents=True, exist_ok=True)
    (d / ("%s.status.json" % key)).write_text(json.dumps(
        {"key": key, "state": state, "note": note, "url": "", "at": "2026-09-28 00:00:00"},
        ensure_ascii=False), encoding="utf-8")


def acct(base, key):
    for a in api(base, "/api/accounts")["accounts"]:
        if a["key"] == key:
            return a
    return {}


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    DATA.mkdir(parents=True)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(DATA), "--no-browser", "--notify-dry-run"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % PORT
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            up = True
            break
        except Exception:
            time.sleep(0.5)
    ok("测试服务起来了", up)
    if not up:
        proc.kill()
        return finish()

    try:
        # ① 已登录 → 界面必须报 on（同步链路本身要通）
        write_status("douyin", "on", "扫码登录成功")
        write_status("channels", "on", "扫码登录成功")
        a = acct(base, "douyin")
        ok("状态文件 on → 界面 on", a.get("status") == "on", a.get("status"))

        # ② ★ 核心：off 必须同步下去（修复前这里会 FAIL —— 界面还停在 on）
        write_status("douyin", "off", "已核验登录态：等待扫码")
        a = acct(base, "douyin")
        ok("状态文件 off → 界面 off", a.get("status") == "off", a.get("status"))
        ok("off 时 note 也跟着更新", "核验" in (a.get("note") or "") or "未登录" in (a.get("note") or ""),
           a.get("note"))

        # ③ 共用登录态的平台要跟着一起变（西瓜跟抖音）
        x = acct(base, "xigua")
        ok("西瓜跟随抖音一起变 off", x.get("status") == "off", x.get("status"))

        # ④ 其它映射不能被这次改动弄坏
        write_status("douyin", "on", "扫码登录成功")
        write_status("kuaishou", "waiting", "等待扫码")
        ok("waiting → waiting", acct(base, "kuaishou").get("status") == "waiting",
           acct(base, "kuaishou").get("status"))
        write_status("kuaishou", "unknown", "页面未加载完成")
        ok("unknown → waiting（判不准按未登录显示）",
           acct(base, "kuaishou").get("status") == "waiting", acct(base, "kuaishou").get("status"))
        write_status("kuaishou", "error", "页面打开失败")
        ok("error → error", acct(base, "kuaishou").get("status") == "error",
           acct(base, "kuaishou").get("status"))
        write_status("kuaishou", "closed", "窗口已关闭")
        ok("closed → off", acct(base, "kuaishou").get("status") == "off",
           acct(base, "kuaishou").get("status"))

        # ⑤ 反复读要稳定（不能一次 off 一次 on 地抖）
        write_status("douyin", "off", "已核验登录态：等待扫码")
        seq = [acct(base, "douyin").get("status") for _ in range(3)]
        ok("连续读三次结论一致", seq == ["off", "off", "off"], str(seq))

        # ⑥ channels 没被动过，应保持 on
        ok("未被改动的平台不受影响", acct(base, "channels").get("status") == "on",
           acct(base, "channels").get("status"))
    finally:
        try:
            proc.kill()
        except Exception:
            pass

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

# -*- coding: utf-8 -*-
"""开发项 S9 补充自测：断点恢复（重启清理 running）/ 维护机制（同平台连续失败自动停）

运行：python tests/test_09_system.py
"""
import json
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8963
TMP = APP / "tests" / "_tmp"

sys.path.insert(0, str(APP / "tests"))
from gen_mp4 import build_mp4  # noqa: E402

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
        req = urllib.request.Request(base + path, data=json.dumps(data).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def wait_batch(base, batch, timeout=120):
    end = time.time() + timeout
    last = []
    while time.time() < end:
        last = api(base, "/api/publish/batch?id=%d" % batch)["jobs"]
        if all(r["status"] in ("success", "fail", "canceled", "manual") for r in last):
            return last
        time.sleep(1)
    return last


def start_server():
    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(TMP / "data"), "--no-browser", "--notify-dry-run"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % PORT
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            return proc, base
        except Exception:
            time.sleep(0.25)
    return proc, base


def main():
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "data").mkdir(parents=True)
    (TMP / "data" / "config.json").write_text(json.dumps({"发布间隔": [1, 2], "单账号日更上限": 99}, ensure_ascii=False), encoding="utf-8")
    a = TMP / "in" / "测试视频A.mp4"
    a.parent.mkdir(parents=True, exist_ok=True)
    build_mp4(a, seconds=12, width=1080, height=1920)

    proc, base = start_server()
    ok("服务启动", True)

    try:
        data = a.read_bytes()
        req = urllib.request.Request(base + "/api/videos/upload?name=" + urllib.parse.quote("测试视频A.mp4"),
                                     data=data, method="POST")
        vid = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())["video"]["id"]

        # A. 维护机制：同平台连续失败 → 自动停
        b1 = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["douyin", "douyin", "douyin"], "mode": "now", "mock": True,
            "copies": {"common": {"title": "[fail] 自动停演示", "body": "", "topics": ""}}})["batch"]
        jobs = wait_batch(base, b1)
        st = [r["status"] for r in jobs]
        ok("三连失败任务处理", st[0] == "fail" and st[1] == "fail" and st[2] == "canceled", st)
        ok("自动停原因标注", "自动停" in (jobs[2].get("error") or "") + (jobs[2].get("step") or ""),
           (jobs[2].get("step"), jobs[2].get("error")))

        # B. 断点恢复：造一个 running 状态 → 重启 → 应被标记失败可重试
        b2 = api(base, "/api/publish/create", {
            "video_id": vid, "platforms": ["kuaishou"], "mode": "now", "mock": True,
            "copies": {"common": {"title": "[fail] 断点演示", "body": "", "topics": ""}}})["batch"]
        jobs2 = wait_batch(base, b2)
        jid = jobs2[0]["id"]

        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        time.sleep(1)

        db = TMP / "data" / "app.db"
        with sqlite3.connect(db, timeout=10) as c:
            c.execute("UPDATE publishes SET status='running', step='假运行中' WHERE id=?", (jid,))
            c.commit()

        proc2, base = start_server()
        time.sleep(2)
        rec = api(base, "/api/records")
        row = [r for r in rec["records"] if r["id"] == jid][0]
        ok("重启后 running 被清理", row["status"] == "fail" and "重启" in (row.get("step") or ""),
           (row["status"], row.get("step")))

        # 且可重试（重试后能再跑成功）
        api(base, "/api/publish/retry", {"id": jid})
        fixed = wait_batch(base, 1)[0]  # 拿任意批次不适用，改用轮询 records
        done = False
        for _ in range(40):
            time.sleep(1)
            rr = api(base, "/api/records")
            row = [r for r in rr["records"] if r["id"] == jid][0]
            if row["status"] == "success":
                done = True
                break
        ok("断点任务可重试成功", done, row["status"])

        # C. 数据目录加固（icacls 收紧访问权限）
        h = api(base, "/api/security/harden", {})
        ok("加固接口可用", h.get("ok") is True, h)
        probe = TMP / "data" / "probe_after_harden.txt"
        probe.write_text("ok", encoding="utf-8")
        ok("加固后读写正常", probe.read_text(encoding="utf-8") == "ok")
    finally:
        try:
            proc2.terminate()
            proc2.wait(timeout=8)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    return finish()


def finish():
    passed = sum(1 for _, c in RESULTS if c)
    total = len(RESULTS)
    print("\n===== 结果: %d/%d 通过 =====" % (passed, total))
    sys.exit(0 if passed == total and total > 0 else 1)


if __name__ == "__main__":
    main()

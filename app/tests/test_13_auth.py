# -*- coding: utf-8 -*-
"""v1.7 自测：访问鉴权（服务端会话）—— 部署到服务器前的硬前提

运行：python tests/test_13_auth.py

★★ 背景：本工具原本绑 127.0.0.1、**没有任何鉴权** —— 那是「本机桌面应用」的前提。
   一旦部署到服务器对外提供地址，任何人（不只是本机）都能删素材、改配置、
   触发真实企微/飞书推送（审查报告 2.5）。本测试验证：
     ① 没设口令时保持本机模式，不拦（不破坏原来的单机用法）
     ② 设了口令后，**所有业务接口都要求登录**（不只是前端画个遮罩）
     ③ 会话用 HttpOnly + SameSite=Lax 的 Cookie —— 顺带挡住 CSRF/DNS-rebinding
     ④ Host 头白名单（防 DNS rebinding）
     ⑤ 绑非本机地址却没设口令 → **拒绝启动**
"""
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8981
TMP = APP / "tests" / "_tmp"

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def call(base, path, data=None, token="", host=None):
    """返回 (status, body_dict_or_text, headers)。不自动跟随重定向，方便看 401/403。"""
    hdr = {}
    if data is not None:
        hdr["Content-Type"] = "application/json"
    if token:
        hdr["Cookie"] = "vp_session=" + token
    if host:
        hdr["Host"] = host
    req = urllib.request.Request(base + path,
                                 data=json.dumps(data).encode("utf-8") if data is not None else None,
                                 headers=hdr, method="POST" if data is not None else "GET")
    try:
        with OPENER.open(req, timeout=15) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw), dict(r.headers)
            except Exception:
                return r.status, raw, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), dict(e.headers)
        except Exception:
            return e.code, raw, dict(e.headers)


def main():
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    (TMP / "data").mkdir(parents=True)

    logf = open(TMP / "server.out.txt", "wb")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(TMP / "data"), "--no-browser"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % PORT
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            up = True
            break
        except Exception:
            time.sleep(0.25)
    ok("服务启动", up)
    if not up:
        proc.terminate()
        return finish()

    try:
        # ── ① 未设口令：本机模式，不拦（保持原有单机用法）──
        s, j, _ = call(base, "/api/videos")
        ok("未设口令时不拦（本机模式）", s == 200 and j.get("ok") is True, (s, j))
        s, j, _ = call(base, "/api/auth/status")
        ok("状态接口显示未设口令", j.get("口令已设") is False and j.get("logged_in") is False, j)

        # ── ② 设口令 ──
        s, j, _ = call(base, "/api/config/save", {"data": {"口令": "S3cret!pw"}})
        ok("口令设置成功", s == 200 and j.get("config", {}).get("口令已设") is True, j)

        # ── ③ 设了口令之后，业务接口必须要求登录 ──
        for path in ("/api/videos", "/api/config", "/api/accounts", "/api/records", "/api/metrics"):
            s, j, _ = call(base, path)
            if not (s == 401 and j.get("need_login")):
                ok("未登录访问 %s 被拒" % path, False, (s, j))
                break
        else:
            ok("未登录访问所有业务接口都被拒（401）", True)

        # 写接口同样要拦（不能只拦读）
        s, j, _ = call(base, "/api/videos/delete", {"id": 1})
        ok("未登录调用写接口被拒（401）", s == 401, (s, j))
        s, j, _ = call(base, "/api/security/harden", {})
        ok("未登录调用敏感接口被拒（401）", s == 401, (s, j))

        # 媒体接口也在保护内（封面/截图）
        s, j, _ = call(base, "/api/videos/cover/1")
        ok("未登录取媒体也被拒（401）", s == 401, s)

        # ── ④ 登录：错口令 / 对口令 ──
        s, j, _ = call(base, "/api/auth/login", {"pass": "wrong"})
        ok("错口令被拒", s == 401 and j.get("ok") is False, (s, j))

        s, j, hdr = call(base, "/api/auth/login", {"pass": "S3cret!pw"})
        ok("对口令登录成功", s == 200 and j.get("ok") is True, (s, j))
        sc = hdr.get("Set-Cookie", "")
        ok("下发了会话 Cookie", "vp_session=" in sc, sc)
        ok("Cookie 带 HttpOnly（JS 读不到）", "HttpOnly" in sc, sc)
        ok("Cookie 带 SameSite=Lax（挡 CSRF/DNS-rebinding）", "SameSite=Lax" in sc, sc)
        token = ""
        for part in sc.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "vp_session":
                token = v
        ok("取到会话令牌", bool(token))

        # ── ⑤ 带会话就能访问 ──
        s, j, _ = call(base, "/api/videos", token=token)
        ok("带会话访问业务接口成功", s == 200 and j.get("ok") is True, (s, j))
        s, j, _ = call(base, "/api/config", token=token)
        ok("带会话读配置成功", s == 200, s)

        # ── ⑥ Host 头白名单（防 DNS rebinding）──
        s, j, _ = call(base, "/api/videos", token=token, host="evil.example.com")
        ok("陌生域名 Host 被拒（403）", s == 403, (s, j))
        s, j, _ = call(base, "/api/videos", token=token, host="127.0.0.1:%d" % PORT)
        ok("IP 字面量 Host 放行", s == 200, s)

        # ── ⑦ 登出后会话失效 ──
        s, j, _ = call(base, "/api/auth/logout", {}, token=token)
        ok("登出成功", s == 200, s)
        s, j, _ = call(base, "/api/videos", token=token)
        ok("登出后原会话失效（401）", s == 401, s)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

    # ── ⑧ 绑非本机 + 无口令 → 拒绝启动（最关键的防呆）──
    TMP2 = APP / "tests" / "_tmp_auth2"
    if TMP2.exists():
        shutil.rmtree(TMP2, ignore_errors=True)
    (TMP2 / "data").mkdir(parents=True)
    r = subprocess.run([sys.executable, str(APP / "server.py"), "--host", "0.0.0.0",
                        "--port", "8982", "--data-dir", str(TMP2 / "data"), "--no-browser"],
                       cwd=str(APP), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    ok("绑对外地址但没设口令 → 拒绝启动", r.returncode == 2, "rc=%s out=%s" % (r.returncode, (r.stdout or "")[:120]))
    ok("拒绝启动时给出可操作的提示", "访问口令" in (r.stdout or ""), (r.stdout or "")[:160])

    # 设了口令就应该能起来
    # ★ 2026-10-06（安全审查 R2）：口令现在住在 auth.json，不再在 config.json 里。
    # ★ 光断言"进程活着"是**恒真**的 —— 本机监听压根不检查有没有口令，
    #   口令写错位置它照样起来（改了文件位置后这条断言仍然会 PASS，但什么都没验到）。
    #   所以补一条能证明"服务真的认了这个口令"的断言：带口令时业务接口必须 401。
    (TMP2 / "data" / "auth.json").write_text(json.dumps(
        {"口令哈希": "x", "口令盐": "y", "口令版本": 1}, ensure_ascii=False), encoding="utf-8")
    p2 = subprocess.Popen([sys.executable, str(APP / "server.py"), "--host", "127.0.0.1",
                           "--port", "8983", "--data-dir", str(TMP2 / "data"), "--no-browser"],
                          cwd=str(APP), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    alive = p2.poll() is None
    requires_login = False
    if alive:
        try:
            with urllib.request.urlopen("http://127.0.0.1:8983/api/videos", timeout=5) as rr:
                requires_login = (rr.status == 401)
        except urllib.error.HTTPError as e:
            requires_login = (e.code == 401)
        except Exception:
            pass
    p2.terminate()
    ok("有口令时正常启动（127.0.0.1）", alive)
    ok("启动后确实要求登录（口令被认到，而非把文件写错了位置）", requires_login)
    shutil.rmtree(TMP2, ignore_errors=True)

    return finish()


def finish():
    passed = sum(1 for _, c in RESULTS if c)
    total = len(RESULTS)
    print("\n===== 结果: %d/%d 通过 =====" % (passed, total))
    sys.exit(0 if passed == total and total > 0 else 1)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()

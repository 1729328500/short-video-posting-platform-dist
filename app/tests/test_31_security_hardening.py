# -*- coding: utf-8 -*-
"""安全加固自测（2026-10-06 审查报告 R1–R9）

运行：cd app && python -X utf8 tests/test_31_security_hardening.py

对应设计 13-安全加固-设计.md / 实施计划 13-安全加固-实施计划.md。
覆盖：请求来源校验（R1）、口令独立存放与损坏失败关闭（R2）、
改口令撤销旧会话（R3）、上传不持全局锁与限额（R5）、
CSV 公式前缀（R6）、更新源 HTTPS（R7）、登录正向确认（R9）。
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PORT = 8990
TMP = APP / "tests" / "_tmp31"
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


def cookie_of(headers):
    sc = headers.get("Set-Cookie", "") if headers else ""
    for part in sc.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "vp_session":
            return v.strip()
    return ""


def call(base, path, data=None, token="", host=None, origin=None, ctype=None):
    """返回 (status, body, headers)。不跟随重定向，方便看 401/403/415。"""
    hdr = {}
    if data is not None:
        hdr["Content-Type"] = ctype or "application/json"
    if token:
        hdr["Cookie"] = "vp_session=" + token
    if host:
        hdr["Host"] = host
    if origin:
        hdr["Origin"] = origin
    req = urllib.request.Request(
        base + path,
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


def boot(data_dir):
    """起一个服务实例，返回 (proc, base, logf)。"""
    logf = open(TMP / "server.out.txt", "ab")
    proc = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT),
         "--data-dir", str(data_dir), "--no-browser"],
        cwd=str(APP), stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%d" % PORT
    for _ in range(40):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1)
            return proc, base, logf
        except Exception:
            time.sleep(0.25)
    return proc, base, logf


def stop(proc, logf):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    logf.close()


def finish():
    passed = sum(1 for _, c in RESULTS if c)
    total = len(RESULTS)
    print("\n===== 结果: %d/%d 通过 =====" % (passed, total))
    sys.exit(0 if passed == total and total > 0 else 1)


def test_auth_file():
    """R2：口令独立存放 + 首次启动/损坏的区分。"""
    d = TMP / "t1"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    proc, base, logf = boot(d / "data")
    try:
        # ① 没有任何 auth.json → 首次启动，本机模式放行
        s, j, _ = call(base, "/api/videos")
        ok("无 auth.json 时本机模式放行", s == 200 and j.get("ok") is True, (s, j))

        # ② 设口令 → auth.json 落盘，config.json 不含口令字段
        s, j, _ = call(base, "/api/config/save", {"data": {"口令": "S3cret!pw"}})
        ok("设口令成功", s == 200 and j.get("config", {}).get("口令已设") is True, (s, j))
        auth = d / "data" / "auth.json"
        cfg = d / "data" / "config.json"
        ok("auth.json 已生成", auth.is_file())
        ra = json.loads(auth.read_text(encoding="utf-8")) if auth.is_file() else {}
        ok("auth.json 含口令哈希", bool(ra.get("口令哈希")), ra)
        rc = json.loads(cfg.read_text(encoding="utf-8")) if cfg.is_file() else {}
        ok("config.json 不再含口令字段",
           "口令哈希" not in rc and "口令盐" not in rc, rc)
    finally:
        stop(proc, logf)

    # ③ auth.json 损坏 → 业务接口必须 503，不能退回免密码模式
    auth.write_text("{坏掉的 JSON", encoding="utf-8")
    proc, base, logf = boot(d / "data")
    try:
        s, j, _ = call(base, "/api/videos")
        ok("auth.json 损坏时业务接口 503", s == 503, (s, j))
        s, j, _ = call(base, "/api/auth/status")
        ok("状态接口报告配置损坏", s == 200 and j.get("配置损坏") is True, (s, j))
    finally:
        stop(proc, logf)

    # ④ auth.json 是合法 JSON 但没有口令字段 → 按"无口令"处理
    d2 = TMP / "t2"
    if d2.exists():
        shutil.rmtree(d2, ignore_errors=True)
    (d2 / "data").mkdir(parents=True)
    (d2 / "data" / "auth.json").write_text("{}", encoding="utf-8")
    proc, base, logf = boot(d2 / "data")
    try:
        s, j, _ = call(base, "/api/videos")
        ok("auth.json 为空对象时按无口令放行", s == 200, (s, j))
    finally:
        stop(proc, logf)


def test_migration():
    """旧格式：口令在 config.json 里 → 迁到 auth.json 并移除。"""
    d = TMP / "t3"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    # 口令哈希用服务自己的算法生成，避免写死不可复现的值
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    import server as srv
    cfg = {}
    srv.set_password(cfg, "S3cret!pw")
    (d / "data" / "config.json").write_text(
        json.dumps({"口令哈希": cfg["口令哈希"], "口令盐": cfg["口令盐"],
                    "发布间隔": [60, 180]}, ensure_ascii=False), encoding="utf-8")
    proc, base, logf = boot(d / "data")
    try:
        s, j, _ = call(base, "/api/auth/status")
        ok("迁移后状态显示已设口令", j.get("口令已设") is True, j)
        s, j, _ = call(base, "/api/videos")
        ok("迁移后未登录被拒（口令确实生效）", s == 401, (s, j))
        auth = d / "data" / "auth.json"
        ok("迁移生成 auth.json", auth.is_file())
        rc = json.loads((d / "data" / "config.json").read_text(encoding="utf-8"))
        ok("迁移后 config.json 已移除口令字段", "口令哈希" not in rc, rc)
        ok("迁移保留其它配置项（发布间隔）",
           rc.get("发布间隔") == [60, 180], rc)
    finally:
        stop(proc, logf)


def test_config_broken():
    """config.json 损坏 → 应用照常可用（本机模式）+ 备份 + 界面可见；
    auth.json 同时也坏 → 必须以拒绝服务为准。"""
    d = TMP / "t4"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    (d / "data" / "config.json").write_text("{坏掉的配置", encoding="utf-8")
    proc, base, logf = boot(d / "data")
    try:
        s, j, _ = call(base, "/api/videos")
        ok("config.json 损坏时本机模式仍可用", s == 200, (s, j))
        s, j, _ = call(base, "/api/health")
        ok("健康检查报告配置损坏", j.get("配置损坏") is True, j)
        broken = list((d / "data").glob("config.json.broken-*"))
        ok("损坏的 config.json 已备份", len(broken) == 1, broken)
    finally:
        stop(proc, logf)

    # 两个文件同时损坏 → 拒绝服务（不能因为 config 能兜底就放行）
    d2 = TMP / "t5"
    if d2.exists():
        shutil.rmtree(d2, ignore_errors=True)
    (d2 / "data").mkdir(parents=True)
    (d2 / "data" / "config.json").write_text("{坏", encoding="utf-8")
    (d2 / "data" / "auth.json").write_text("{也坏", encoding="utf-8")
    proc, base, logf = boot(d2 / "data")
    try:
        s, j, _ = call(base, "/api/videos")
        ok("两个配置文件同时损坏时拒绝服务", s == 503, (s, j))
    finally:
        stop(proc, logf)


def test_session_revocation():
    """R3：改口令必须撤销所有旧会话；对外监听不得运行中清空口令。"""
    d = TMP / "t6"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    proc, base, logf = boot(d / "data")
    try:
        call(base, "/api/config/save", {"data": {"口令": "pw-one"}})
        s, j, h1 = call(base, "/api/auth/login", {"pass": "pw-one"})
        t1 = cookie_of(h1)
        s, j, h2 = call(base, "/api/auth/login", {"pass": "pw-one"})
        t2 = cookie_of(h2)
        ok("两个会话都建立", bool(t1) and bool(t2) and t1 != t2, (t1, t2))
        s, j, _ = call(base, "/api/videos", token=t1)
        ok("改口令前旧会话可用", s == 200, (s, j))

        # ★ 改口令这个请求本身要带会话：设了口令之后 /api/config/save 就在鉴权后面了
        #   （真实用户在设置页改口令时正是登录态）。不带 token 会先吃一个 401。
        s, j, h3 = call(base, "/api/config/save", {"data": {"口令": "pw-two"}}, token=t1)
        t3 = cookie_of(h3)
        ok("改口令成功并下发新会话", s == 200 and bool(t3), (s, j, h3))

        s, j, _ = call(base, "/api/videos", token=t1)
        ok("旧会话①改口令后失效（401）", s == 401, (s, j))
        s, j, _ = call(base, "/api/videos", token=t2)
        ok("旧会话②改口令后失效（401）", s == 401, (s, j))
        s, j, _ = call(base, "/api/videos", token=t3)
        ok("操作人新会话仍可用", s == 200, (s, j))
        s, j, _ = call(base, "/api/auth/login", {"pass": "pw-one"})
        ok("旧口令不再能登录", s == 401, (s, j))
    finally:
        stop(proc, logf)

    # 对外监听时运行中清空口令 → 必须 400
    d2 = TMP / "t7"
    if d2.exists():
        shutil.rmtree(d2, ignore_errors=True)
    (d2 / "data").mkdir(parents=True)
    logf2 = open(d2 / "server.out.txt", "wb")
    setup = subprocess.run(
        [sys.executable, str(APP / "server.py"), "--set-password", "pw-one",
         "--data-dir", str(d2 / "data")],
        cwd=str(APP), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60)
    ok("--set-password 设口令成功", setup.returncode == 0,
       (setup.returncode, (setup.stdout or "")[:120]))
    proc2 = subprocess.Popen(
        [sys.executable, str(APP / "server.py"), "--port", str(PORT), "--host", "0.0.0.0",
         "--data-dir", str(d2 / "data"), "--no-browser"],
        cwd=str(APP), stdout=logf2, stderr=subprocess.STDOUT)
    base2 = "http://127.0.0.1:%d" % PORT
    up = False
    for _ in range(40):
        try:
            urllib.request.urlopen(base2 + "/api/health", timeout=1)
            up = True
            break
        except Exception:
            time.sleep(0.25)
    try:
        ok("对外监听服务已启动", up)
        s, j, hdr = call(base2, "/api/auth/login", {"pass": "pw-one"})
        tok = cookie_of(hdr)
        ok("对外监听下登录成功", s == 200 and bool(tok), (s, j))
        s, j, _ = call(base2, "/api/config/save", {"data": {"口令": ""}}, token=tok)
        ok("对外监听时清空口令被拒（400）", s == 400, (s, j))
        s, j, _ = call(base2, "/api/videos", token=tok)
        ok("清空被拒后原会话仍有效", s == 200, (s, j))
    finally:
        stop(proc2, logf2)


def test_origin_gate():
    """R1：跨站来源被拒、同源放行、非浏览器调用方放行、裸 Content-Type 被拒。"""
    d = TMP / "t8"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    proc, base, logf = boot(d / "data")
    try:
        # ★ 本机免密码模式（默认）正是审查报告复现攻击的场景 —— 来源校验必须在这一档也生效
        s, j, _ = call(base, "/api/config/save", {"data": {"单账号日更上限": 3}},
                       origin="http://evil.example")
        ok("跨站来源改设置被拒（403）", s == 403, (s, j))

        s, j, _ = call(base, "/api/config/save", {"data": {"单账号日更上限": 3}},
                       origin="http://127.0.0.1:%d" % PORT)
        ok("同源来源放行", s == 200, (s, j))

        s, j, _ = call(base, "/api/config/save", {"data": {"单账号日更上限": 4}})
        ok("无 Origin 的非浏览器调用方放行", s == 200, (s, j))

        s, j, _ = call(base, "/api/config/save", {"data": {"单账号日更上限": 5}},
                       origin="null")
        ok("Origin: null 被拒（不是「没有 Origin」）", s == 403, (s, j))

        s, j, _ = call(base, "/api/config/save", {"data": {"单账号日更上限": 6}},
                       ctype="text/plain")
        ok("text/plain 打 JSON 接口被拒（415）", s == 415, (s, j))

        s, j, _ = call(base, "/api/videos", origin="http://evil.example")
        ok("跨站读取被拒（403）", s == 403, (s, j))

        # 登录接口也必须纳入来源策略（防"登录 CSRF"）
        s, j, _ = call(base, "/api/auth/login", {"pass": "x"}, origin="http://evil.example")
        ok("跨站打登录接口被拒（403）", s == 403, (s, j))

        # 裸 body 接口：来源校验仍然生效，但不受 JSON Content-Type 规则影响
        s, j, _ = call(base, "/api/videos/upload?name=a.webm", {"x": 1},
                       origin="http://evil.example")
        ok("跨站上传被拒（403）", s == 403, (s, j))
        s, j, _ = call(base, "/api/videos/upload?name=a.webm", {"x": 1}, ctype="video/webm")
        ok("裸 body 接口不被 415 拦下（进到业务逻辑）", s != 415, (s, j))
    finally:
        stop(proc, logf)


def raw_post(path, extra_headers, body=b"", read_reply=True):
    """裸 socket 发请求，返回 (conn, status)。status 只读首行；不跟随任何东西。"""
    import socket as _s
    conn = _s.create_connection(("127.0.0.1", PORT), timeout=15)
    head = "POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n" % (path, PORT)
    for k, v in extra_headers.items():
        head += "%s: %s\r\n" % (k, v)
    head += "\r\n"
    conn.sendall(head.encode("ascii"))
    if body:
        conn.sendall(body)
    status = 0
    if read_reply:
        try:
            data = conn.recv(4096).decode("utf-8", "replace")
            if data.startswith("HTTP/"):
                status = int(data.split(" ")[1])
        except Exception:
            pass
    return conn, status


def test_upload_limits():
    """R5：上传读网络时不持全局锁；超限 413；中断后临时文件被清理。"""
    d = TMP / "t9"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    proc, base, logf = boot(d / "data")
    try:
        # ① 正常上传仍然可用
        s, j, _ = call(base, "/api/videos/upload?name=ok.webm", {"x": 1}, ctype="video/webm")
        ok("正常上传成功", s == 200 and j.get("ok") is True, (s, j))

        # ② 声明超限 → 413（按 Content-Length 先判，不读 body）
        conn, st = raw_post("/api/videos/upload?name=big.webm",
                            {"Content-Type": "video/webm",
                             "Content-Length": str(5 * 1024 * 1024 * 1024)})
        conn.close()
        ok("超过上传上限被拒（413）", st == 413, st)

        # ③ 慢上传**还挂着**的时候，另一个写接口必须能立刻完成。
        #    ★ 这条是 R5 的核心证据：旧实现持锁读网络，这里会一直卡到慢连接结束。
        #      （"断开后锁被释放"证明不了什么 —— 旧实现断开后也会释放。）
        #      client 必须用裸 socket：urllib 会强制按 body 长度写 Content-Length，
        #      发不出"声明 1 MiB 只给 10 字节"这种慢请求。
        slow, _ = raw_post("/api/videos/upload?name=slow.webm",
                           {"Content-Type": "video/webm",
                            "Content-Length": str(1048576)},
                           body=b"0123456789", read_reply=False)
        time.sleep(0.5)
        t0 = time.time()
        try:
            s, j, _ = call(base, "/api/config/save", {"data": {"单账号日更上限": 3}})
        except Exception as e:            # 超时也是"卡住"，算失败
            s, j = 0, str(e)
        dt = time.time() - t0
        ok("慢上传挂起时写接口仍能在 5 秒内完成", s == 200 and dt < 5.0,
           "status=%s 用时=%.2fs %s" % (s, dt, j))

        # ④ 断开慢连接 → 上传中止，且不留临时文件
        slow.close()
        time.sleep(1.5)
        parts = list((d / "data" / "videos").glob(".*.part"))
        ok("中断上传不留临时文件", len(parts) == 0, parts)

        # ⑤ JSON 体超限 → 413
        big = {"data": {"播报时间": "x" * (5 * 1024 * 1024)}}
        s, j, _ = call(base, "/api/config/save", big)
        ok("超大 JSON 请求体被拒（413）", s == 413, (s, j))
    finally:
        stop(proc, logf)


def test_csv_formula():
    """R6：外部内容进 CSV 时必须中和公式前缀。"""
    d = TMP / "t10"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    proc, base, logf = boot(d / "data")
    try:
        # 直接往数据库塞一条带公式的发布记录（标题来自外部内容）
        import sqlite3
        with sqlite3.connect(d / "data" / "app.db") as c:
            c.execute("INSERT INTO publishes(batch,video_id,platform,title,status,error,"
                      "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                      (1, 1, "douyin", '=HYPERLINK("http://evil.example","点我")', "fail",
                       "+1+1", "2026-10-06 12:00:00", "2026-10-06 12:00:00"))
        s, body, hdr = call(base, "/api/publish/export")
        ok("导出返回 200", s == 200, s)
        ok("公式标题被加前缀（不以 = 开头）",
           '"=HYPERLINK' not in body and "'=HYPERLINK" in body, body[:220])
        ok("公式说明被加前缀（不以 + 开头）",
           '"+1+1"' not in body and '"\'+1+1"' in body, body[:220])
    finally:
        stop(proc, logf)


def test_login_classify():
    """R9：维护页/未知页不判成功；unknown 不被写成 on 也不被写成 off。"""
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    import platform_login as pl

    NORMAL = "一切正常的一页普通内容，既没有登录页特征词，也没有已登录的提示词，就是很普通的一页。"

    cases = [
        ("系统正在维护中，请稍后再试。给您带来的不便敬请谅解。", "unknown",
         "维护页不判已登录"),
        ("欢迎回来，这是你的创作者中心，可以发布视频了", "on",
         "命中已登录特征 → on"),
        ("扫码登录  请使用今日头条App扫码登录", "waiting",
         "登录页 → waiting"),
        (NORMAL, "unknown",
         "正文正常但没有任何特征 → unknown（不再默认 on）"),
        ("ERR_CONNECTION_REFUSED 无法访问此网站", "unknown",
         "错误页 → unknown"),
    ]
    for text, want, name in cases:
        st, note, detail = pl.classify(text, "https://creator.douyin.com/")
        ok(name, st == want, (st, note, detail))

    # 账号接口确认过 → 即使页面看不出任何特征也算 on
    st, note, detail = pl.classify(NORMAL, "https://creator.douyin.com/", positive=True)
    ok("账号接口确认 → on", st == "on", (st, note, detail))
    # 正向确认要能压过"正文太短"这一档：账号接口都说登录了，页面长短不该否决
    st, _, _ = pl.classify("短", "https://creator.douyin.com/", positive=True)
    ok("账号接口确认压过正文长度检查", st == "on", st)
    # 但"确定未登录"必须优先于正向确认 —— 否则登录页 + 旧 cookie 会被判成已登录
    st, _, _ = pl.classify("扫码登录", "https://creator.douyin.com/", positive=True)
    ok("登录页特征优先于正向确认（waiting）", st == "waiting", st)

    # 核验写回策略：判不准必须保留原状态（不能把已登录账号写成 off）
    ok("核验 on → 写 on", pl.verify_state("on") == "on")
    ok("核验 waiting → 写 off", pl.verify_state("waiting") == "off")
    ok("核验 unknown → 保留原状态（None）", pl.verify_state("unknown") is None,
       pl.verify_state("unknown"))


def test_update_scheme():
    """R7：公网 http 更新源被拒；私网需显式开关；开关只对私网生效。"""
    if str(APP.parent) not in sys.path:
        sys.path.insert(0, str(APP.parent))
    from launcher import update as up

    for bad in ("http://example.com/version.json", "http://8.8.8.8/v.json"):
        try:
            up._http_url(bad)
            ok("公网 http 源被拒：%s" % bad, False, "没有报错")
        except ValueError:
            ok("公网 http 源被拒：%s" % bad, True)

    try:
        up._http_url("http://192.168.1.10/version.json")
        ok("私网 http 源默认也被拒", False, "没有报错")
    except ValueError:
        ok("私网 http 源默认也被拒", True)

    os.environ["VP_UPDATE_ALLOW_HTTP"] = "1"
    try:
        ok("显式开关后私网 http 放行",
           up._http_url("http://192.168.1.10/version.json").startswith("http://"))
        try:
            up._http_url("http://example.com/version.json")
            ok("开关只对私网生效（公网 http 仍被拒）", False, "没有报错")
        except ValueError:
            ok("开关只对私网生效（公网 http 仍被拒）", True)
    finally:
        os.environ.pop("VP_UPDATE_ALLOW_HTTP", None)

    ok("https 正常放行", up._http_url("https://example.com/v.json").startswith("https://"))

    # 带凭据的地址一律拒绝（防"看起来是 https，其实带 user:pass"）
    try:
        up._http_url("https://user:pw@example.com/v.json")
        ok("带凭据的更新源被拒", False, "没有报错")
    except ValueError:
        ok("带凭据的更新源被拒", True)


def _inproc(data_dir):
    """进程内起一个服务 —— 竞态测试需要能 patch 模块级函数做受控交错。

    子进程模式（boot()）做不到这一点：patch 打在子进程里无效。
    """
    import http.server
    import threading
    import server as app

    store = app.Store(data_dir)

    class H(app.Handler):
        def log_message(self, *_):
            pass

    H.store = store
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return app, store, srv, "http://127.0.0.1:%d" % srv.server_port


def _set_auth(store, app, pw, ver):
    auth = {"口令版本": ver}
    app.set_password(auth, pw)
    auth["更新时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
    app.save_auth(store, auth)
    return auth


def test_auth_race_login_snapshot():
    """复核发现1a：登录必须用**同一份**认证快照。

    否则：旧密码通过检查后、取版本时重新读一次，读到的是改口令后的新版本
    —— 于是拿旧密码登录能换到一个改口令后仍然有效的会话。
    """
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    d = TMP / "t11"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    app, store, srv, base = _inproc(d / "data")
    real_load = app.load_auth
    try:
        _set_auth(store, app, "old-pw", 1)

        # 受控交错：第 1 次读返回旧认证（旧密码通过），之后返回改口令后的新认证
        calls = {"n": 0}

        def flipping_load(st):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_load(st)
            return _set_auth(store, app, "new-pw", 2)   # 模拟此刻另一请求改了口令

        app.load_auth = flipping_load
        s, j, hdr = call(base, "/api/auth/login", {"pass": "old-pw"})
        tok = cookie_of(hdr)
        app.load_auth = real_load
        # ★ 无论上面的交错有没有真的发生，这里都把口令改掉 —— 断言的是
        #   "这次登录发出的会话，在口令变更之后还能不能用"。
        #   （不补这一步的话，修复后第二次读不会发生、口令也就没变，
        #     那个会话当然是有效的 —— 断言会通过得没有区分力。GREEN 阶段实测到。）
        _set_auth(store, app, "new-pw", 2)

        if tok:
            s2, _, _ = call(base, "/api/videos", token=tok)
            ok("旧密码登录拿到的会话在改口令后无效", s2 == 401,
               "只读一次快照就该带旧版本号；拿到 200 说明版本取自第二次读")
        else:
            ok("旧密码登录拿到的会话在改口令后无效", True)   # 直接 401 更好
        ok("认证读取只发生一次（快照）", calls["n"] == 1, "实际读取 %d 次" % calls["n"])
    finally:
        app.load_auth = real_load
        srv.shutdown()
        srv.server_close()


def test_auth_write_single_transaction():
    """复核发现1b：哈希/盐/版本必须在**同一次**认证保存里更新。

    否则：先写新哈希、再单独 bump 版本 —— 第二次写失败就留下
    「密码已经变了、旧会话却没被撤销」的状态。
    """
    if str(APP) not in sys.path:
        sys.path.insert(0, str(APP))
    d = TMP / "t12"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    app, store, srv, base = _inproc(d / "data")
    real_save = app.save_auth
    try:
        _set_auth(store, app, "old-pw", 1)
        s, j, hdr = call(base, "/api/auth/login", {"pass": "old-pw"})
        t_old = cookie_of(hdr)
        s, _, _ = call(base, "/api/videos", token=t_old)
        ok("改口令前旧会话可用", s == 200, s)

        # 让第 2 次认证写入失败（模拟"先写哈希成功、再 bump 版本失败"）
        n = {"c": 0}

        def failing_save(st, a):
            n["c"] += 1
            if n["c"] >= 2:
                raise OSError("模拟认证写入失败")
            return real_save(st, a)

        app.save_auth = failing_save
        try:
            call(base, "/api/config/save", {"data": {"口令": "new-pw"}}, token=t_old)
        except Exception:
            pass
        app.save_auth = real_save

        # 不变式：不许同时出现「密码已变」和「旧会话仍有效」
        cur = app.load_auth(store)
        pw_changed = not app.check_password(cur, "old-pw")   # 旧密码不再可用＝密码已变
        s_old, _, _ = call(base, "/api/videos", token=t_old)
        old_valid = (s_old == 200)
        ok("认证写入只发生一次（单事务）", n["c"] <= 1, "实际写入 %d 次" % n["c"])
        ok("没有「密码已变 + 旧会话仍有效」的组合", not (pw_changed and old_valid),
           (pw_changed, old_valid, cur.get("口令版本")))
    finally:
        app.save_auth = real_save
        srv.shutdown()
        srv.server_close()


def test_plaintext_migration_boundary():
    """复核发现2：明文口令迁移不得覆盖已有 auth.json，也不得在写入失败时放行。"""
    d = TMP / "t13"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    # auth.json 已是"新密码 + 版本 5"，config.json 里还留着旧明文
    sys.path.insert(0, str(APP))
    import server as srvmod
    _set_auth(srvmod.Store(d / "data"), srvmod, "new-pw", 5)
    (d / "data" / "config.json").write_text(
        json.dumps({"口令": "stale-plaintext", "单账号日更上限": 7}, ensure_ascii=False),
        encoding="utf-8")
    proc, base, logf = boot(d / "data")
    try:
        s, j, _ = call(base, "/api/auth/status")
        ok("已有 auth.json 时口令已设", j.get("口令已设") is True, j)
        # 旧明文不能把版本打回去（否则此前失效的会话会被重新激活）
        s, j, hdr = call(base, "/api/auth/login", {"pass": "stale-plaintext"})
        ok("遗留明文不能重新登录", s == 401, (s, j))
        s, j, hdr = call(base, "/api/auth/login", {"pass": "new-pw"})
        ok("auth.json 里的口令仍然有效", s == 200, (s, j))
        ra = json.loads((d / "data" / "auth.json").read_text(encoding="utf-8"))
        ok("版本没有被明文迁移打回 1", int(ra.get("口令版本") or 0) == 5, ra.get("口令版本"))
    finally:
        stop(proc, logf)


def test_cover_upload_no_lock():
    """复核发现3：封面上传也不能持全局锁读网络。"""
    d = TMP / "t14"
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    (d / "data").mkdir(parents=True)
    proc, base, logf = boot(d / "data")
    try:
        s, j, _ = call(base, "/api/videos/upload?name=c.webm", {"x": 1}, ctype="video/webm")
        vid = (j.get("video") or {}).get("id") if isinstance(j, dict) else None
        ok("先传一个素材用于挂封面", s == 200 and vid, (s, j))

        # 慢封面：声明 1 MiB 只发 10 字节，挂着不关
        slow, _ = raw_post("/api/videos/cover?id=%s" % vid,
                           {"Content-Type": "image/jpeg", "Content-Length": str(1048576)},
                           body=b"0123456789", read_reply=False)
        time.sleep(0.5)
        t0 = time.time()
        try:
            s, j, _ = call(base, "/api/config/save", {"data": {"单账号日更上限": 3}})
        except Exception as e:
            s, j = 0, str(e)
        dt = time.time() - t0
        ok("慢封面挂起时写接口仍能在 5 秒内完成", s == 200 and dt < 5.0,
           "status=%s 用时=%.2fs %s" % (s, dt, j))
        slow.close()
        time.sleep(1.5)
        left = [p.name for p in (d / "data" / "covers").glob("*") if p.name.startswith(".")]
        ok("中断的封面不留临时文件", left == [], left)
    finally:
        stop(proc, logf)


def test_update_private_host_strict():
    """复核发现4：私网判断必须真解析 IP，不能拿域名前缀当证据。

    ★ 必须打开 VP_UPDATE_ALLOW_HTTP，否则所有 http 都被拒、下面每条都会"通过" ——
      那是通过得不是地方：这条要测的是**地址判定**，不是"http 默认被拒"。
      （写第一版时就是这么错的，RED 阶段发现。）
    """
    if str(APP.parent) not in sys.path:
        sys.path.insert(0, str(APP.parent))
    from launcher import update as up

    os.environ["VP_UPDATE_ALLOW_HTTP"] = "1"
    try:
        for bad in ("10.example.com", "127.evil.com", "192.168.attacker.com",
                    "172.16.evil.com", "10.0.0.1.example.com"):
            try:
                up._http_url("http://%s/version.json" % bad)
                ok("看起来像私网的域名被拒：%s" % bad, False, "没有报错")
            except ValueError:
                ok("看起来像私网的域名被拒：%s" % bad, True)

        for good in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.3.4", "localhost", "[::1]"):
            try:
                up._http_url("http://%s/version.json" % good)
                ok("真实私网/环回地址放行：%s" % good, True)
            except ValueError as e:
                ok("真实私网/环回地址放行：%s" % good, False, e)

        for badip in ("172.15.0.1", "172.32.0.1", "11.0.0.1"):
            try:
                up._http_url("http://%s/version.json" % badip)
                ok("非私网字面量被拒：%s" % badip, False, "没有报错")
            except ValueError:
                ok("非私网字面量被拒：%s" % badip, True)
    finally:
        os.environ.pop("VP_UPDATE_ALLOW_HTTP", None)


def main():
    if TMP.exists():
        shutil.rmtree(TMP, ignore_errors=True)
    TMP.mkdir(parents=True)
    test_auth_file()
    test_migration()
    test_config_broken()
    test_session_revocation()
    test_origin_gate()
    test_upload_limits()
    test_csv_formula()
    test_login_classify()
    test_update_scheme()
    # 2026-10-06 复核（security-review-20261006）发现的边界
    test_auth_race_login_snapshot()
    test_auth_write_single_transaction()
    test_plaintext_migration_boundary()
    test_cover_upload_no_lock()
    test_update_private_host_strict()
    finish()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()

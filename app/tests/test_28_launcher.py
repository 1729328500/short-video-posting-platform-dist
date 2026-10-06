"""Offline update, crash recovery and packaging acceptance. Run sequentially."""
import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from launcher import update as up
from launcher.common import atomic_json, validate_data_path
from launcher.locking import FileLock
from launcher.package import build
from launcher.install import configure

RESULTS = []


def ok(name, condition, extra=""):
    RESULTS.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + name + (" | " + str(extra) if not condition else ""), flush=True)


def finish():
    print("\n===== 结果: %d/%d 通过 =====" % (sum(RESULTS), len(RESULTS)))
    return 0 if all(RESULTS) else 1


def fixture(app, version="1.0.0"):
    app.mkdir(parents=True, exist_ok=True)
    (app / "ui").mkdir(exist_ok=True)
    for name, body in {"server.py": 'APP_VERSION = "%s"\n' % version, "VERSION": version,
                       "ui/index.html": "<html>OK</html>", "ui/app.js": "// OK", "ui/style.css": "body{}"}.items():
        (app / name).write_text(body, encoding="utf-8")


def bundle(version="2.0.0", extra=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in {"server.py": 'APP_VERSION="%s"\n' % version, "VERSION": version,
                           "ui/index.html": "<html>OK</html>", "ui/app.js": "// OK", "ui/style.css": "body{}", **(extra or {})}.items():
            info = zipfile.ZipInfo(name)
            info.filename = name  # ZipInfo constructor normalizes backslashes on Windows.
            z.writestr(info, text)
    return stream.getvalue()


def manifest(body, version="2.0.0", **extra):
    return dict(version=version, zip="app-%s.zip" % version, size=len(body),
                md5=hashlib.md5(body).hexdigest(), sha256=hashlib.sha256(body).hexdigest(), **extra)


ROUTES, REQUESTS = {}, []


class Source(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        REQUESTS.append(self.path)
        body = ROUTES.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *args):
        pass


def source(prefix, body, man=None):
    man = man or manifest(body)
    ROUTES[prefix + "/version.json"] = json.dumps(man).encode()
    ROUTES[prefix + "/" + man["zip"]] = body
    return BASE + prefix


def run():
    global BASE
    with tempfile.TemporaryDirectory(prefix="_tmp28-", dir=ROOT / "app/tests") as temp:
        temp = Path(temp)
        root = temp / "package"
        app = root / "app"
        fixture(app)
        snapshots = {}
        for name in ("data/video.mp4", "data/qrlogin/state.json", "runtime/marker", "browsers/marker", "install.json"):
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"KEEP")
            snapshots[p] = (p.read_bytes(), p.stat().st_mtime_ns)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Source)
        BASE = "http://127.0.0.1:%d" % server.server_port
        # ★ 2026-10-06（安全审查 R7）：更新源默认只允许 HTTPS，明文 HTTP 需要
        #   显式打开 VP_UPDATE_ALLOW_HTTP 且目标必须是私网地址（这里是 127.0.0.1 回环）。
        #   本测试的源就在本机，正是那个「受控内网源」场景。
        os.environ["VP_UPDATE_ALLOW_HTTP"] = "1"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            body = bundle()
            good = source("/good", body)
            for name, field, value in [("越界路径", "zip", "../data"), ("绝对路径", "zip", "C:/data.zip"),
                                       ("反斜杠", "zip", "a\\b.zip"), ("size缺失", "size", None),
                                       ("md5缺失", "md5", None), ("布尔size", "size", True),
                                       ("凭据URL", "url", "https://user:pass@example.org/a.zip")]:
                d = manifest(body)
                if value is None:
                    d.pop(field)
                else:
                    d[field] = value
                try:
                    up.parse_manifest(json.dumps(d))
                    rejected = False
                except ValueError:
                    rejected = True
                ok("清单拒绝" + name, rejected)
            same = source("/same", bundle("1.0.0"), manifest(bundle("1.0.0"), "1.0.0"))
            REQUESTS.clear()
            r = up.apply_update(app, [same])
            ok("同版本不下载", not r["updated"] and all(not p.endswith('.zip') for p in REQUESTS))
            bad = source("/bad", body, dict(manifest(body), md5="0" * 32))
            r = up.apply_update(app, [bad])
            ok("校验失败保留旧版并清理临时文件", not r["updated"] and up.read_local_version(app) == "1.0.0" and not (root / up.DOWNLOAD).exists())
            corrupt = source("/corrupt", b"not a zip")
            ok("校验正确但ZIP损坏仍保留旧版", not up.apply_update(app, [corrupt])["updated"])
            fallback = up.apply_update(app, [bad, good])
            ok("清单可达但ZIP损坏会换源", fallback["updated"] and fallback["source"] == good)
            ok("更新保留数据配置环境与时间戳", all((p.read_bytes(), p.stat().st_mtime_ns) == snap for p, snap in snapshots.items()))
            ok("未健康确认的更新下次离线启动恢复", up.recover_update(app) and up.read_local_version(app) == "1.0.0")
            up.apply_update(app, [same, good])
            up.confirm_update(app)
            ok("首源过旧也能使用第二源新版", up.read_local_version(app) == "2.0.0")
            ok("确认后下次启动不回退", not up.recover_update(app) and up.read_local_version(app) == "2.0.0")
            ok("不降级", not up.apply_update(app, [same])["updated"] and up.read_local_version(app) == "2.0.0")
            ok("可回滚", up.rollback(app) and up.read_local_version(app) == "1.0.0")
            for attack in ("../outside.txt", "data/keep", "runtime/python.exe", "ui/CON.txt", "ui/a. ", "C:/bad", "ui\\bad", "UI/APP.JS"):
                hostile = source("/hostile", bundle(extra={attack: "bad"}))
                r = up.apply_update(app, [hostile])
                ok("拒绝ZIP路径 " + attack, not r["updated"] and up.read_local_version(app) == "1.0.0")
            with patch.object(up, '_safe_extract', side_effect=OSError('磁盘满')):
                r = up.apply_update(app, [good])
            ok("解压磁盘满保留旧版", not r['updated'] and not (root / 'app.new').exists())
            with patch.object(up, 'atomic_json', side_effect=OSError('只读')):
                r = up.apply_update(app, [good])
            ok("替换写日志失败保留旧版", not r['updated'] and up.read_local_version(app) == '1.0.0')
            original_remove = up.remove_managed
            def locked_download(parent, name):
                if name == up.DOWNLOAD:
                    raise PermissionError('下载临时文件被占用')
                return original_remove(parent, name)
            with patch.object(up, 'remove_managed', locked_download):
                r = up.apply_update(app, [good])
            ok('临时文件清理失败不丢失更新事务状态', r['updated'] and (root / up.JOURNAL).exists())
            up.rollback(app)
            if os.name == 'nt':
                junction = root / 'app.new'
                created = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(junction), str(root / 'data')], capture_output=True).returncode == 0
                try:
                    r = up.apply_update(app, [good]) if created else {}
                    ok('Windows目录联接不能让更新触碰数据', created and not r.get('updated') and (root / 'data/video.mp4').read_bytes() == b'KEEP')
                finally:
                    if created:
                        os.rmdir(junction)  # Remove the junction itself, never its target.
            newer = source('/incompatible', body, dict(manifest(body), min_launcher=99))
            ok("不支持的启动器协议不会替换", not up.apply_update(app, [newer])['updated'])
            atomic_json(root / 'runtime/manifest.json', {'dependency_lock_sha256': 'a' * 64})
            ok("运行环境不兼容要求完整包", not up.apply_update(app, [good])['updated'])
            (root / 'runtime/manifest.json').unlink()
            mismatched = source('/wrong-lock', body, dict(manifest(body), dependency_lock_sha256='b' * 64))
            ok('包内依赖锁必须与清单一致', not up.apply_update(app, [mismatched])['updated'])
            up.mark_failed(app, '2.0.0')
            ok("失败版本不会反复更新", not up.apply_update(app, [good])['updated'])
            (root / up.QUARANTINE).unlink()
            lock = FileLock(root / '.update.lock')
            with lock:
                code = subprocess.run([sys.executable, '-X', 'utf8', '-c',
                    'import sys;from launcher.locking import FileLock;l=FileLock(sys.argv[1]);sys.exit(0 if not l.acquire() else 1)', str(root / '.update.lock')], cwd=ROOT).returncode
            ok("进程之间更新锁互斥", code == 0)
            # Kill a real process immediately after each rename, not just a raised exception.
            for count in (1, 2):
                crashroot = temp / ('crash%d' % count)
                fixture(crashroot / 'app')
                fixture(crashroot / 'app.new', '2.0.0')
                code = subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__)), '--crash', str(crashroot), str(count)], cwd=ROOT).returncode
                restored = up.recover_update(crashroot / 'app')
                ok("第%d次重命名后强杀可离线恢复" % count, code == 37 and restored and up.read_local_version(crashroot / 'app') == '1.0.0')
            app2 = temp / 'packing/app'
            fixture(app2)
            for name in ('data/password.txt', 'tests/private.py', 'notes.txt', 'private.json', 'private.py', '.env', 'random.bak'):
                p = app2 / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('PRIVATE')
            (app2 / 'VERSION').write_text('0.0.1')
            m = build(app2, temp / 'dist', '')
            with zipfile.ZipFile(m['zip']) as z:
                names = z.namelist()
                ok("打包白名单排除数据测试杂项", not any('PRIVATE' in z.read(n).decode(errors='ignore') for n in names))
                ok("打包重新生成过期VERSION并回读", z.read('VERSION').strip() == b'1.0.0')
            (app2 / 'server.py').write_text('APP_VERSION="1.1.0"')
            ok("正常升版本可再次打包", build(app2, temp / 'dist', '')['version'] == '1.1.0')
            (app2 / 'ui/app.js').write_text('// changed')
            try:
                build(app2, temp / 'dist', '')
                rejected = False
            except ValueError:
                rejected = True
            ok("拒绝同版本覆盖不同内容", rejected)
            for forbidden in (root, root / 'app/data', root / 'runtime', root.parent):
                try:
                    validate_data_path(forbidden, root); rejected = False
                except ValueError:
                    rejected = True
                ok("数据路径不与程序重叠 " + forbidden.name, rejected)
            installroot = temp / 'install'
            installroot.mkdir()
            atomic_json(installroot / 'install.json', {'data_dir': str(installroot / 'data'), 'update_sources': [], 'custom': 'keep'})
            cfg = configure(installroot)
            ok("重复安装保留数据路径空更新源和自定义配置", cfg['custom'] == 'keep' and cfg['update_sources'] == [] and cfg['data_dir'] == str(installroot / 'data'))
        finally:
            server.shutdown(); server.server_close()
    return finish()


if __name__ == '__main__':
    if '--crash' in sys.argv:
        root, count = Path(sys.argv[2]), int(sys.argv[3])
        original = Path.rename
        done = [0]
        def crash_rename(self, target):
            result = original(self, target)
            done[0] += 1
            if done[0] == count:
                os._exit(37)
            return result
        with FileLock(root / '.update.lock'), patch.object(Path, 'rename', crash_rename):
            up.swap_in(root / 'app', root / 'app.new')
    else:
        sys.exit(run())

"""Desktop service ownership, UI mode, rollback and owned process tree acceptance."""
import http.server
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from launcher import start, update
from launcher.common import atomic_json, read_json
from launcher.package import build, production_files
from launcher.processes import ProcessTree
from launcher.locking import FileLock
from test_28_launcher import ok, finish, manifest


def get(port, path='/api/health'):
    with urllib.request.urlopen('http://127.0.0.1:%d%s' % (port, path), timeout=1) as r:
        return json.loads(r.read())


def wait_meta(path, proc):
    for _ in range(100):
        meta = read_json(path)
        if meta and start.probe(meta):
            return meta
        if proc.poll() is not None:
            raise RuntimeError('launcher exited early: %s' % proc.returncode)
        time.sleep(.1)
    raise RuntimeError('service did not become ready')


def run():
    with tempfile.TemporaryDirectory(prefix='_tmp29-', dir=ROOT / 'app/tests') as temp:
        root = Path(temp) / '中文绿色包'
        root.mkdir()
        for file, name in production_files(ROOT / 'app'):
            dest = root / 'app' / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, dest)
        (root / 'app/VERSION').write_text(update.read_app_version(root / 'app'))
        shutil.copytree(ROOT / 'launcher', root / 'launcher', ignore=shutil.ignore_patterns('__pycache__'))
        occupied = socket.socket()
        occupied.bind(('127.0.0.1', 0)); occupied.listen()
        preferred = occupied.getsockname()[1]
        if preferred > 65530:
            raise RuntimeError('chosen port outside launcher test range')
        atomic_json(root / 'install.json', {'data_dir': '用户数据', 'update_sources': [], 'port': preferred})
        data = root / '用户数据'
        # ★ 2026-10-06（安全审查 R7）：更新源默认只允许 HTTPS，明文 HTTP 需要显式打开
        #   VP_UPDATE_ALLOW_HTTP 且目标必须是环回/私网（这里是 127.0.0.1 的本机测试源）。
        #   必须在构造 env 之前设 —— 子进程继承的是这一步的 os.environ。
        os.environ['VP_UPDATE_ALLOW_HTTP'] = '1'
        env = dict(os.environ, VP_BROWSER_CHANNEL='chromium')
        argv = [sys.executable, '-X', 'utf8', str(root / 'launcher/start.py'), '--dev', '--no-browser', '--mock-login', 'douyin']
        log = (root / 'test.log').open('wb')
        owner = ProcessTree(argv, cwd=root, env=env, stdout=log, stderr=log)
        try:
            meta = wait_meta(root / '.desktop.instance.json', owner.proc)
            port = meta['port']
            h = get(port)
            ok('端口占用后使用实际端口和当前实例身份', port != preferred and h['instance_id'] == meta['instance_id'] and h['data_id'] == meta['data_id'])
            ok('相对中文数据路径按包根目录解析', (data / 'app.db').exists() and not (root / 'launcher/用户数据').exists())
            ok('桌面健康信息不暴露noVNC且使用本机登录', h['mode'] == 'desktop' and h['login_mode'] == 'local' and h['novnc_port'] == 0)
            # Seed a running job while the original engine is alive; a duplicate must not recover it.
            with sqlite3.connect(data / 'app.db') as c:
                c.execute("INSERT INTO publishes(batch,platform,status,created_at,updated_at) VALUES(99,'douyin','running','test','test')")
            c.close()
            duplicate = subprocess.run(argv, cwd=root, env=env, capture_output=True, timeout=10)
            ok('重复启动只使用原实例', duplicate.returncode == 0 and str(port).encode() in duplicate.stdout and read_json(root / '.desktop.instance.json')['pid'] == meta['pid'])
            direct = subprocess.run([sys.executable, '-X', 'utf8', str(root / 'app/server.py'), '--desktop', '--no-browser', '--port', str(port + 5), '--data-dir', str(data)], cwd=root, env=env, capture_output=True, timeout=10)
            with sqlite3.connect(data / 'app.db') as c:
                state = c.execute('SELECT status FROM publishes WHERE batch=99').fetchone()[0]
            c.close()
            ok('直接重复启动在数据库恢复之前被拒绝', direct.returncode == 3 and state == 'running')
            (data / 'update.status.json').write_text('broken JSON')
            ok('损坏的更新状态不导致健康接口失败', get(port)['update'] == {})
            atomic_json(data / 'update.status.json', {'result': 'rolled-back', 'version': h['version'], 'checked_at': 'test'})
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                errors, requests = [], []
                page.on('pageerror', lambda e: errors.append(str(e)))
                page.on('request', lambda r: requests.append(r.url))
                page.goto('http://127.0.0.1:%d/#accounts' % port)
                page.wait_for_function("document.querySelector('#accountHint').textContent.includes('本机浏览器')")
                ok('桌面UI展示本机登录与回滚状态', '已恢复旧版' in page.locator('#verLine').inner_text() and '所选数据目录' in page.locator('#storageHint').inner_text())
                page.locator('.acard[data-key="douyin"] .act-login').click()
                page.wait_for_timeout(500)
                ok('桌面登录按钮直达本机接口', any('/api/accounts/login' in r for r in requests) and not any('/api/accounts/qr/start' in r for r in requests))
                ok('桌面UI无JS错误', not errors, errors)
                browser.close()
        except Exception:
            print((root / 'test.log').read_text(encoding='utf-8', errors='replace'), end='')
            if (data / 'logs/server.log').exists():
                print((data / 'logs/server.log').read_text(encoding='utf-8', errors='replace')[-1500:], end='')
            raise
        finally:
            owner.stop(); log.close(); occupied.close()
        ok('关闭启动器后服务退出且数据保存', not start.probe(meta) and (data / 'app.db').exists())
        # Binding a busy port must not create/reset a database even in legacy server mode.
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0)); busy.listen()
            untouched = root / 'bind-failure-data'
            result = subprocess.run([sys.executable, str(root / 'app/server.py'), '--no-browser', '--port', str(busy.getsockname()[1]), '--data-dir', str(untouched)], capture_output=True, timeout=10)
            ok('端口绑定失败不创建数据库或打开不明网页', result.returncode == 4 and not (untouched / 'app.db').exists())
        # A valid update whose backend crashes must be rolled back before the old backend restarts.
        badapp = Path(temp) / 'bad/app'
        badapp.mkdir(parents=True); (badapp / 'ui').mkdir()
        (badapp / 'server.py').write_text('APP_VERSION="99.0.0"\nraise RuntimeError("bad release")\n')
        (badapp / 'ui/index.html').write_text('<html>bad</html>')
        (badapp / 'ui/app.js').write_text('// bad')
        (badapp / 'ui/style.css').write_text('body{}')
        badman = build(badapp, Path(temp) / 'release', '')
        body = badman['zip'].read_bytes()
        routes = {'/dist/version.json': json.dumps(manifest(body, '99.0.0')).encode(), '/dist/app-99.0.0.zip': body}
        class Source(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = routes.get(self.path, b'')
                self.send_response(200 if body else 404); self.end_headers(); self.wfile.write(body)
            def log_message(self, *args): pass
        source = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Source)
        threading.Thread(target=source.serve_forever, daemon=True).start()
        cfg = read_json(root / 'install.json'); cfg['update_sources'] = ['http://127.0.0.1:%d/dist' % source.server_port]
        atomic_json(root / 'install.json', cfg)
        try:
            result = subprocess.run(argv + ['--once', '--health-timeout', '3'], cwd=root, env=env, capture_output=True, timeout=15)
            status = read_json(data / 'update.status.json')
            ok('新版启动失败自动恢复旧版再健康检查', result.returncode == 0 and status.get('result') == 'rolled-back' and update.read_local_version(root / 'app') == h['version'], result.stdout.decode(errors='replace'))
            ok('自动回滚记录失败版本避免循环', read_json(root / update.QUARANTINE).get('version') == '99.0.0')
        finally:
            source.shutdown(); source.server_close()
        if os.name == 'nt':
            with (root / 'force-stop.log').open('wb') as stoplog:
                raw = subprocess.Popen(argv + ['--no-updates'], cwd=root, env=env, stdout=stoplog, stderr=stoplog)
            try:
                lastmeta = wait_meta(root / '.desktop.instance.json', raw)
                raw.kill(); raw.wait(timeout=5)
                unlocked = False
                for _ in range(50):
                    lock = FileLock(data / '.desktop.instance.lock')
                    if lock.acquire():
                        lock.release(); unlocked = True; break
                    time.sleep(.1)
                ok('强杀启动器也结束后端并释放数据锁', unlocked and not start.probe(lastmeta))
            finally:
                if raw.poll() is None:
                    raw.kill(); raw.wait(timeout=5)
        # Repeated local login must reuse the same profile owner and preserve pre-login state.
        sys.path.insert(0, str(ROOT / 'app'))
        import server as backend
        store = backend.Store(root / 'login-test')
        store.set_account('douyin', 'on', '原有登录态')
        class FakeWorker:
            exited = False
            def poll(self): return 0 if self.exited else None
        worker = FakeWorker()
        def fake_start(*args):
            backend.WORKERS['douyin'] = worker
            return {'key': 'douyin', 'state': 'opening'}
        try:
            with patch.object(backend, '_start_local_worker', side_effect=fake_start) as spawn:
                backend.start_worker(store, 'douyin')
                backend.start_worker(store, 'douyin')
                ok('重复本机登录只创建一个浏览器会话', spawn.call_count == 1 and backend.WORKERS['douyin'] is worker)
            worker.exited = True
            closed = backend.stop_worker(store, 'douyin')
            ok('中止重新登录保留原有登录态', closed['state'] == 'on' and backend.read_worker_status(store, 'douyin')['note'] == '原有登录态')
        finally:
            backend.WORKERS.pop('douyin', None)
        # A different HTTP application cannot pass readiness even when it returns ok=true.
        class Unrelated(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true,"app":"other"}')
            def log_message(self, *args): pass
        unrelated = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Unrelated)
        threading.Thread(target=unrelated.serve_forever, daemon=True).start()
        try:
            ok('普通HTTP200不能冒充当前服务', not start.probe(dict(meta, port=unrelated.server_port)))
        finally:
            unrelated.shutdown(); unrelated.server_close()
        with patch.object(start, 'webbrowser') as wb:
            wrong = dict(meta, instance_id='wrong')
            ok('身份不匹配不打开浏览器', not start.probe(wrong) and not wb.open.called)
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            pidfile = root / 'child.pid'
            script = "import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)"
            tree = ProcessTree([sys.executable, '-c', script, str(pidfile)])
            for _ in range(50):
                if pidfile.exists(): break
                time.sleep(.1)
            pid = int(pidfile.read_text())
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x100000, False, pid)
            try:
                tree.stop()
                ok('Windows Job结束连带清理子进程', handle and kernel.WaitForSingleObject(handle, 5000) == 0)
            finally:
                if handle: kernel.CloseHandle(handle)
                tree.stop()
    return finish()


if __name__ == '__main__':
    sys.exit(run())

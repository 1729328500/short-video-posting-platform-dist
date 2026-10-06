"""Login success closes all tabs before reporting on, and persists cookies."""
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'app'))
from launcher.processes import ProcessTree
import login_worker as worker
import browser_state
from test_28_launcher import ok, finish


def status(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


class LoginSite(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/ready':
            body = '<h1>创作中心 首页 内容管理 数据中心 发布视频 账号设置 退出登录 你已登录</h1><script>localStorage.setItem("auth_token","saved");const r=indexedDB.open("auth",1);r.onupgradeneeded=()=>r.result.createObjectStore("tokens");r.onsuccess=()=>r.result.transaction("tokens","readwrite").objectStore("tokens").put("saved","token");setTimeout(()=>document.cookie="late_cookie=saved;Path=/;Max-Age=3600",4500)</script>'
        elif self.path == '/protected':
            logged = 'session_login=saved' in self.headers.get('Cookie', '')
            body = 'authenticated' if logged else 'please sign in'
        elif self.path == '/popup':
            body = '<h1>扫码登录 密码登录 验证码登录</h1><script>setTimeout(()=>window.open("/ready"),500)</script>'
        else:
            body = '<h1>扫码登录 密码登录 验证码登录</h1>'
            if self.path == '/login':
                body += '<script>setTimeout(()=>location.href="/ready",500)</script>'
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        if self.path == '/ready':
            self.send_header('Set-Cookie', 'login_cookie=saved; Path=/; Max-Age=3600; HttpOnly')
            self.send_header('Set-Cookie', 'session_login=saved; Path=/; HttpOnly')
        self.end_headers(); self.wfile.write(body.encode('utf-8'))

    def log_message(self, *args): pass


def run():
    with tempfile.TemporaryDirectory(prefix='_tmp30-', dir=ROOT / 'app/tests') as temporary:
        root = Path(temporary)
        # Terminal success is emitted strictly AFTER close, keeping the same account metadata.
        output = root / 'unit.json'
        args = SimpleNamespace(key='douyin', status=str(output), profile=str(root / 'unit-profile'))
        page, context = Mock(), Mock()
        context.storage_state.return_value = {'cookies': [], 'origins': []}
        def closing():
            ok('关闭浏览器之前不提前标记已登录', status(output).get('state') == 'waiting')
        context.close.side_effect = closing
        with patch.object(worker, 'probe_account', return_value=('account-123', '测试账号')):
            worker.finish_login(context, page, args, 'https://example.org/creator', '检测到已登录', '稳定检测通过')
        saved = status(output)
        ok('自动关闭后保留账号ID和名称', saved['account'] == 'account-123' and saved['account_name'] == '测试账号' and saved['browser_closed'] and saved['state'] == 'on')
        context = Mock(); context.storage_state.return_value = {'cookies': [], 'origins': []}
        context.close.side_effect = RuntimeError('close failed')
        with patch.object(worker, 'probe_account', return_value=('', '')):
            try:
                worker.finish_login(context, page, args, 'https://example.org/creator', 'OK', 'OK')
                rejected = False
            except RuntimeError:
                rejected = True
        ok('关闭失败不谎报profile已释放', rejected and status(output)['state'] != 'on')
        context = Mock(); context.storage_state.side_effect = OSError('disk full')
        with patch.object(worker, 'probe_account', return_value=('', '')):
            try:
                worker.finish_login(context, page, args, 'https://example.org/creator', 'OK', 'OK')
                rejected = False
            except OSError:
                rejected = True
        ok('登录态保存失败不报告成功', rejected and status(output)['state'] != 'on')
        probe_page = Mock()
        probe_page.url = 'https://creator.douyin.com/'
        worker.probe_account(probe_page, 'douyin')
        ok('账号资料请求有超时不会长时间阻塞关窗', probe_page.request.get.call_args.kwargs.get('timeout') == 3000)
        cookie = {'name': 'shared', 'value': 'old', 'domain': 'example.org', 'path': '/', 'expires': -1}
        snapshot = browser_state.state_path(args.profile)
        snapshot.write_text(json.dumps({'version': 1, 'state': {'cookies': [cookie,
            dict(cookie, name='needed'), dict(cookie, name='expired', expires=1),
            dict(cookie, name='deleted_persistent', expires=time.time() + 1000)], 'origins': []}}), encoding='utf-8')
        ctx = Mock(); ctx.cookies.return_value = [dict(cookie, value='new')]
        browser_state.restore(ctx, args.profile)
        ok('恢复只补缺失的session cookie，不覆盖新token或复活已删除/过期cookie', [c['name'] for c in ctx.add_cookies.call_args.args[0]] == ['needed'])
        ctx = Mock(); browser_state.restore(ctx, root / 'another-platform')
        ok('不同平台的登录态互相隔离', not ctx.add_cookies.called)
        before = snapshot.read_bytes()
        ctx = Mock(); ctx.storage_state.return_value = {'cookies': [], 'origins': []}
        with patch.object(browser_state.os, 'replace', side_effect=OSError('disk full')):
            try:
                browser_state.save(ctx, args.profile)
            except OSError:
                pass
        ok('保存失败保留原备份并清理临时文件', snapshot.read_bytes() == before and not list(root.glob('*.storage.json.*.tmp')))
        snapshot.write_text('{broken', encoding='utf-8')
        ctx = Mock(); browser_state.restore(ctx, args.profile)
        ok('备份损坏不会清空原有profile', not ctx.clear_cookies.called and not ctx.add_cookies.called)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), LoginSite)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = 'http://127.0.0.1:%d' % server.server_port
        env = dict(os.environ, VP_BROWSER_CHANNEL='chromium')
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                for name, route in [('automatic', '/login'), ('popup', '/popup'), ('manual', '/waiting')]:
                    folder = root / name; folder.mkdir()
                    profile, state, cmd = folder / 'profile', folder / 'status.json', folder / 'cmd'
                    with (folder / 'worker.log').open('wb') as log:
                        tree = ProcessTree([sys.executable, '-X', 'utf8', str(ROOT / 'app/login_worker.py'),
                            '--key', 'weibo', '--url', base + route, '--profile', str(profile),
                            '--status', str(state), '--cmd', str(cmd), '--headless', '--timeout', '25'],
                            env=env, stdout=log, stderr=log)
                    observed_early_success = False
                    try:
                        if name == 'manual':
                            for _ in range(50):
                                if status(state).get('state') == 'waiting': break
                                time.sleep(.1)
                            time.sleep(2.5)
                            ok('尚未完成登录时保持窗口等待', tree.proc.poll() is None and status(state).get('state') != 'on')
                            cmd.write_text('confirm', encoding='utf-8')
                        deadline = time.monotonic() + 20
                        while time.monotonic() < deadline and tree.proc.poll() is None:
                            current = status(state)
                            if current.get('state') == 'on' and not current.get('browser_closed'):
                                observed_early_success = True
                            time.sleep(.1)
                        code = tree.proc.poll()
                        final = status(state)
                        ok(name + ' 登录完成后进程自动退出', code == 0 and final.get('state') == 'on' and final.get('browser_closed'), (folder / 'worker.log').read_text(encoding='utf-8'))
                        ok(name + ' 成功状态不会早于自动关窗', not observed_early_success)
                        # Reopen the actual profile: proves it is released, and cookies made it to disk.
                        tree.stop()
                        with browser_state.persistent_context(p.chromium, user_data_dir=profile, headless=True, channel='chromium') as ctx:
                            ok(name + ' 关闭后profile可立即复用', bool(ctx.pages))
                            if name != 'manual':
                                cookies = ctx.cookies(base)
                                ok(name + ' 登录cookie在自动关闭后仍保存', any(c['name'] == 'login_cookie' and c['value'] == 'saved' for c in cookies), cookies)
                                ok(name + ' 宽限期间写入的cookie也保存', any(c['name'] == 'late_cookie' and c['value'] == 'saved' for c in cookies), cookies)
                                pg = ctx.pages[0]
                                pg.goto(base + '/protected')
                                ok(name + ' 浏览器重启后session cookie可直接访问登录接口', pg.inner_text('body') == 'authenticated')
                                ok(name + ' localStorage登录态仍保留', pg.evaluate('localStorage.getItem("auth_token")') == 'saved')
                                token = pg.evaluate('''() => new Promise(resolve => {const r=indexedDB.open('auth');r.onsuccess=()=>{const q=r.result.transaction('tokens').objectStore('tokens').get('token');q.onsuccess=()=>resolve(q.result);};r.onerror=()=>resolve(null);})''')
                                ok(name + ' IndexedDB登录态仍保留', token == 'saved')
                                ctx.clear_cookies()
                        if name != 'manual':
                            with browser_state.persistent_context(p.chromium, user_data_dir=profile, headless=True, channel='chromium') as ctx:
                                ok(name + ' 退出登录后不会从旧备份恢复cookie', not ctx.cookies(base))
                    finally:
                        tree.stop()
        finally:
            server.shutdown(); server.server_close()
    return finish()


if __name__ == '__main__':
    sys.exit(run())

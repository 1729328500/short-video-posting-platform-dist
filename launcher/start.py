"""Recover/update, start one local service, verify identity, then open the UI."""
import argparse
import http.client
import json
import os
import secrets
import socket
import sys
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher import update as up
from launcher.common import atomic_json, data_id, read_json, validate_data_path
from launcher.locking import FileLock
from launcher.processes import ProcessTree

HERE = Path(__file__).resolve().parent.parent
DEFAULT_SOURCES = ["https://gitee.com/guizhou-jubangbang_0/short-video-posting-platform-dist/raw/main/dist"]


def default_data_dir():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "shipin-fabu" / "data"


def load_install(root=HERE):
    p = Path(root) / "install.json"
    if not p.exists():
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("配置必须是对象")
        port = cfg.get("port", 8947)
        if type(port) is not int or not 1 <= port <= 65530:
            raise ValueError("端口配置无效")
        sources = cfg.get("update_sources", DEFAULT_SOURCES)
        if not isinstance(sources, list) or len(sources) > 10:
            raise ValueError("更新源配置无效")
        for source in sources:
            up._http_url(source)
        for key in ("data_dir", "python"):
            if key in cfg and not isinstance(cfg[key], str):
                raise ValueError("%s 必须是路径字符串" % key)
        return cfg
    except (OSError, ValueError, UnicodeError) as e:
        raise ValueError("install.json 读取失败：%s" % e) from e


def check_data_dir(path):
    try:
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=".write-probe-", dir=p):
            pass
        return True, ""
    except OSError as e:
        return False, "数据目录写不了：%s（%s）" % (path, str(e)[:100])


def find_free_port(preferred=8947, span=5):
    for port in range(preferred, min(preferred + span, 65536)):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def probe(meta, timeout=0.4):
    try:
        port = meta["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            return False
        url = "http://127.0.0.1:%d/api/health" % port
        with urllib.request.urlopen(url, timeout=timeout) as response:
            health = json.loads(response.read(65536).decode("utf-8"))
        return (isinstance(health, dict) and health.get("ok") is True and health.get("app") == "短视频发布工具"
                and health.get("instance_id") == meta.get("instance_id")
                and bool(meta.get("instance_id")) and health.get("data_id") == meta.get("data_id"))
    except (OSError, ValueError, KeyError, UnicodeError, http.client.HTTPException):
        return False


def _existing(path, expected_data, open_browser):
    for _ in range(20):
        meta = read_json(path)
        if meta.get("data_id") == expected_data and probe(meta):
            url = "http://127.0.0.1:%d" % meta["port"]
            print("[启动] 工具已在运行：%s" % url)
            if open_browser:
                webbrowser.open(url)
            return True
        time.sleep(0.25)
    print("[启动] 已有实例正在启动或使用该程序，请查看现有窗口")
    return False


def _python(root, cfg, dev):
    configured = cfg.get("python")
    if configured:
        value = Path(configured)
        return str(value if value.is_absolute() else root / value)
    bundled = root / "runtime" / "python" / "python.exe"
    if bundled.is_file():
        return str(bundled)
    if dev:
        return sys.executable
    raise ValueError("运行环境缺失，请完整解压绿色包；源码运行请加 --dev")


def _environment(root, token):
    env = dict(os.environ, VP_INSTANCE_TOKEN=token, PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
    browsers = root / "browsers"
    if browsers.is_dir():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
        env["VP_BROWSER_CHANNEL"] = "chromium"
    env["VP_AUTH_HEADLESS"] = "0"
    env["NOVNC"] = "0"
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    return env


def start_service(root, cfg, data_dir, preferred, timeout, mock_login=""):
    """Only accept our new process's identity, never an unrelated HTTP 200."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / "server.log"
    for offset in range(5):
        port = find_free_port(preferred + offset, 5 - offset)
        if not port:
            break
        token = secrets.token_hex(16)
        meta = {"port": port, "instance_id": token, "data_id": data_id(data_dir)}
        argv = [cfg["python"], "-X", "utf8", str(root / "app" / "server.py"),
                "--desktop", "--no-browser", "--host", "127.0.0.1", "--port", str(port),
                "--data-dir", str(data_dir)]
        if mock_login:
            argv += ["--mock-login", mock_login]
        with log_path.open("ab", buffering=0) as log:
            tree = ProcessTree(argv, cwd=root / "app", env=_environment(root, token), stdout=log, stderr=log)
        ready = False
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if tree.proc.poll() is not None:
                    break
                if probe(meta):
                    meta["pid"] = tree.proc.pid
                    ready = True
                    return tree, meta
                time.sleep(0.1)
            code = tree.proc.poll()
        finally:
            if not ready:
                tree.stop()  # Also clean up on Ctrl+C during readiness.
        if code != 4:  # Only retry a port-bind race; other startup failures need rollback.
            raise RuntimeError("服务启动失败，日志：%s" % log_path)
    raise RuntimeError("没有可用端口，请关闭占用程序后重试")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir")
    parser.add_argument("--port", type=int)
    parser.add_argument("--dev", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-updates", action="store_true")
    parser.add_argument("--once", action="store_true", help="启动健康检查后退出（开发验收）")
    parser.add_argument("--health-timeout", type=float, default=10)
    parser.add_argument("--mock-login", default="")
    args = parser.parse_args(argv)
    tree, bundle_lock = None, None
    root = HERE
    try:
        cfg = load_install(root)
        raw_data = Path(args.data_dir or cfg.get("data_dir") or default_data_dir()).expanduser()
        data_dir = validate_data_path(raw_data if raw_data.is_absolute() else root / raw_data, root)
        good, reason = check_data_dir(data_dir)
        if not good:
            raise ValueError(reason)
        preferred = args.port or cfg.get("port", 8947)
        if not 1 <= preferred <= 65530 or not 0.2 <= args.health_timeout <= 120:
            raise ValueError("端口或健康检查等待时间无效")
        cfg["python"] = _python(root, cfg, args.dev)
        expected_data = data_id(data_dir)
        bundle_lock = FileLock(root / ".launcher.lock")
        if not bundle_lock.acquire():
            _existing(root / ".desktop.instance.json", expected_data, not args.no_browser)
            return 0
        data_lock = FileLock(data_dir / ".desktop.instance.lock")
        if not data_lock.acquire():
            _existing(data_dir / ".desktop.instance.json", expected_data, not args.no_browser)
            return 0
        data_lock.release()
        up.recover_update(root / "app")
        sources = [] if args.no_updates else cfg.get("update_sources", DEFAULT_SOURCES)
        result = up.apply_update(root / "app", sources)
        try:
            tree, meta = start_service(root, cfg, data_dir, preferred, args.health_timeout, args.mock_login)
            if result["updated"]:
                up.confirm_update(root / "app")
        except (OSError, ValueError, RuntimeError) as e:
            if tree:
                tree.stop()
                tree = None
            if not result["updated"] or not up.rollback(root / "app"):
                up.write_status(data_dir, {"result": "startup-failed", "version": up.read_local_version(root / "app"), "message": str(e)})
                raise
            try:
                up.mark_failed(root / "app", result["version"])
            except OSError:
                print("[启动] 无法记录失败版本，请检查程序目录写入权限")
            print("[启动] 新版启动失败，已回滚并重试旧版")
            tree, meta = start_service(root, cfg, data_dir, preferred, args.health_timeout, args.mock_login)
            result = {"reason": "rolled-back", "version": up.read_local_version(root / "app"), "message": "新版启动失败，已恢复旧版"}
        version = up.read_local_version(root / "app") or up.read_app_version(root / "app")
        up.write_status(data_dir, {"result": result["reason"], "version": version, "message": result.get("message", "")})
        atomic_json(root / ".desktop.instance.json", meta)
        url = "http://127.0.0.1:%d" % meta["port"]
        print("[启动] v%s 已在 %s 运行；关闭启动窗口即结束。" % (version, url), flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return 0 if args.once else tree.proc.wait()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError) as e:
        print("[启动] %s" % str(e), flush=True)
        return 1
    finally:
        try:
            if tree:
                tree.stop()
        finally:
            if bundle_lock and bundle_lock.file:
                try:
                    (root / ".desktop.instance.json").unlink(missing_ok=True)
                finally:
                    bundle_lock.release()


if __name__ == "__main__":
    sys.exit(main())

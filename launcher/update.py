"""Verified app-only updates with a recoverable rename transaction."""
import ast
import hashlib
import ipaddress
import http.client
import json
import os
import re
import stat
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from launcher import LAUNCHER_PROTOCOL
from launcher.common import atomic_json, managed_path, read_json, remove_managed
from launcher.locking import AlreadyRunning, FileLock

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
REQUIRED_FILES = ("server.py", "VERSION", "ui/index.html", "ui/app.js", "ui/style.css")
JOURNAL = ".update-transaction.json"
DOWNLOAD = ".update-download.zip"
QUARANTINE = ".update-quarantine.json"


def _root(app_dir):
    raw = Path(app_dir).absolute()
    if raw.name != "app":
        raise ValueError("更新目标必须是 app 目录")
    root = raw.parent.resolve()
    managed_path(root, "app")  # Reject a junction before resolving the app itself.
    return root


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("版本号必须是三段数字")
    return tuple(int(part) for part in value.split("."))


def read_app_version(app_dir):
    tree = ast.parse((Path(app_dir) / "server.py").read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "APP_VERSION" for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                version_key(node.value.value)
                return node.value.value
    raise ValueError("server.py 没有有效的 APP_VERSION")


def read_local_version(app_dir):
    try:
        return (Path(app_dir) / "VERSION").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return ""


def parse_manifest(text):
    d = json.loads(text)
    if not isinstance(d, dict):
        raise ValueError("更新清单必须是对象")
    version_key(d.get("version"))
    name = d.get("zip")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.zip", name):
        raise ValueError("zip 必须是单纯的 ZIP 文件名")
    if type(d.get("size")) is not int or not 0 < d["size"] <= MAX_ARCHIVE:
        raise ValueError("更新清单缺少有效 size 或安装包过大")
    if not isinstance(d.get("md5"), str) or not re.fullmatch(r"[0-9a-fA-F]{32}", d["md5"]):
        raise ValueError("更新清单缺少有效 md5")
    for field in ("sha256", "dependency_lock_sha256"):
        if field in d and (not isinstance(d[field], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", d[field])):
            raise ValueError("更新清单的 %s 无效" % field)
    if type(d.get("min_launcher", 1)) is not int or d.get("min_launcher", 1) < 1:
        raise ValueError("min_launcher 无效")
    if "url" in d:
        _http_url(d["url"])
    return d


def _allow_http():
    """HTTP 源是否被显式允许（默认否）。

    ★ 2026-10-06（安全审查 R7）：清单与包来自同一来源，摘要不构成独立的发布者身份 ——
      明文 HTTP 源上能同时替换清单和包的人，可以让替换后的包通过摘要校验。
      所以默认只走 HTTPS；内网源必须显式打开这个开关，且目标必须是私网地址。
    """
    return (os.environ.get("VP_UPDATE_ALLOW_HTTP") or "").strip().lower() in ("1", "true", "yes")


def _is_private_host(host):
    """环回或私网**地址** —— 只有这类目标才允许配 HTTP 源。

    ★ 2026-10-06 复核发现：原来用 `startswith` 判断，于是 `10.example.com`、
      `127.evil.com`、`192.168.attacker.com` 这类**普通域名**也被当成私网 ——
      打开 VP_UPDATE_ALLOW_HTTP 之后，"只允许内网 HTTP"这条限制形同虚设。
      必须真解析成 IP 再判网段。
    ★ 域名一律不放行：一个域名指向哪里要靠 DNS，而 DNS 不是网络位置的证据
      （唯一例外是 localhost —— 本机保留名，不可能被注册成公网域名）。
    """
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False                     # 不是 IP 字面量 → 不放行
    return ip.is_loopback or ip.is_private


def _http_url(url):
    if not isinstance(url, str):
        raise ValueError("下载地址无效")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("只接受无凭据的 HTTP/HTTPS 地址")
    if parsed.scheme == "http" and not (_allow_http() and _is_private_host(parsed.hostname)):
        raise ValueError("更新源必须使用 HTTPS（内网 HTTP 源需显式设置 VP_UPDATE_ALLOW_HTTP=1）")
    return url


def _manifest(base, timeout=2.0):
    url = _http_url(base.rstrip("/") + "/version.json")
    req = urllib.request.Request(url, headers={"User-Agent": "shipin-fabu-launcher/1", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        # ★ 校验**重定向之后**的最终地址：HTTPS 入口可以被重定向到 HTTP 而无人察觉
        _http_url(response.geturl())
        body = response.read(65537)
    if len(body) > 65536:
        raise ValueError("更新清单过大")
    return parse_manifest(body.decode("utf-8-sig"))


def pick_source(sources, timeout=2.0):
    for base in sources or []:
        try:
            return base.rstrip("/"), _manifest(base, timeout)
        except (OSError, ValueError, TypeError, UnicodeError, http.client.HTTPException):
            continue
    return "", {}


def download(base_url, name, dest, timeout=30.0, url=None):
    """Stream to the caller's fixed temporary file, following Release redirects."""
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.zip", name):
            raise ValueError("非法 ZIP 文件名")
        source = _http_url(url or (base_url.rstrip("/") + "/" + name))
        req = urllib.request.Request(source, headers={"User-Agent": "shipin-fabu-launcher/1"})
        deadline = time.monotonic() + timeout
        with urllib.request.urlopen(req, timeout=timeout) as response, Path(dest).open("wb") as f:
            _http_url(response.geturl())      # 重定向之后的最终地址也必须合规
            size = 0
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError("下载超时")
                chunk = response.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARCHIVE:
                    raise ValueError("下载文件过大")
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        return True
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return False


def verify(path, manifest):
    try:
        man = parse_manifest(json.dumps(manifest))
        p = Path(path)
        if p.stat().st_size != man["size"]:
            return False, "size 不符"
        hashes = {"md5": hashlib.md5(), "sha256": hashlib.sha256()}
        with p.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                for h in hashes.values():
                    h.update(chunk)
        for name, h in hashes.items():
            if name in man and h.hexdigest() != man[name].lower():
                return False, "%s 不符" % name
        return True, ""
    except (OSError, ValueError, TypeError) as e:
        return False, "校验失败：%s" % e


def validate_app(app_dir, version=None, dependency_lock=None):
    p = Path(app_dir)
    for name in REQUIRED_FILES:
        if not (p / name).is_file():
            raise ValueError("程序包缺少 %s" % name)
    actual = read_app_version(p)
    if read_local_version(p) != actual or (version and actual != version):
        raise ValueError("程序包 VERSION、APP_VERSION 与清单版本不一致")
    if (p / "data").exists():
        raise ValueError("程序目录含用户数据，拒绝替换")
    if dependency_lock:
        lock = p / "requirements-desktop.lock"
        if not lock.is_file() or hashlib.sha256(lock.read_bytes()).hexdigest() != dependency_lock.lower():
            raise ValueError("程序包依赖锁与清单不一致")
    return actual


def _safe_extract(zip_path, dest):
    """Validate the entire ZIP before writing; no links, traversal or user data."""
    root = Path(dest).resolve()
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ValueError("ZIP 解压规模超出限制")
        seen = set()
        for info in infos:
            name = info.orig_filename  # Windows ZipInfo normalizes '\\' before exposing filename.
            if not name or "\\" in name or ":" in name or "\0" in name:
                raise ValueError("ZIP 路径无效")
            parts = PurePosixPath(name).parts
            if name.startswith("/") or not parts or any(p in (".", "..") for p in parts):
                raise ValueError("ZIP 路径越界")
            if parts[0].lower() in ("data", "runtime", "browsers", "launcher", "install.json"):
                raise ValueError("ZIP 包含程序以外的目录")
            for part in parts:
                stem = part.split(".")[0].upper()
                if part.rstrip(" .") != part or stem in {"CON", "PRN", "AUX", "NUL", *["COM%d" % n for n in range(1, 10)], *["LPT%d" % n for n in range(1, 10)]}:
                    raise ValueError("ZIP 包含 Windows 非法文件名")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("ZIP 不允许符号链接")
            target = root.joinpath(*parts).resolve()
            identity = target.relative_to(root).as_posix().casefold() if target.is_relative_to(root) else ""
            if not identity or identity in seen:
                raise ValueError("ZIP 路径越界或重复")
            seen.add(identity)
        z.extractall(root)


def _recover(app_dir):
    root = _root(app_dir)
    app = managed_path(root, "app")
    bak = managed_path(root, "app.bak")
    journal_path = managed_path(root, JOURNAL)
    pending = journal_path.exists()
    restored = False
    if not app.exists() and bak.exists():
        validate_app(bak)
        bak.rename(app)
        restored = True
    elif pending and app.exists() and bak.exists():
        phase = read_json(journal_path).get("phase")
        if phase != "prepared":
            validate_app(bak)
            remove_managed(root, "app.failed")
            app.rename(root / "app.failed")
            try:
                bak.rename(app)
            except OSError:
                (root / "app.failed").rename(app)
                raise
            restored = True
    if not app.exists():
        raise ValueError("程序目录缺失且无可用备份")
    for name in ("app.new", "app.failed", DOWNLOAD):
        try:
            remove_managed(root, name)
        except (OSError, ValueError) as e:
            print("[更新] 工作文件暂时无法清理，保留现有程序：%s" % e)
    journal_path.unlink(missing_ok=True)
    return restored


def recover_update(app_dir):
    root = _root(app_dir)
    with FileLock(managed_path(root, ".update.lock")):
        return _recover(app_dir)


def swap_in(app_dir, new_dir):
    root = _root(app_dir)
    app, new, bak = (managed_path(root, name) for name in ("app", "app.new", "app.bak"))
    try:
        if Path(new_dir).resolve() != new:
            raise ValueError("只能替换固定的 app.new 目录")
        validate_app(new)
        if (app / "data").exists():
            raise ValueError("旧程序目录含用户数据，拒绝替换")
        atomic_json(managed_path(root, JOURNAL), {"phase": "prepared"})
        remove_managed(root, "app.bak")
        app.rename(bak)
        atomic_json(root / JOURNAL, {"phase": "backed-up"})
        new.rename(app)
        atomic_json(root / JOURNAL, {"phase": "installed"})
        return True, ""
    except (OSError, ValueError, SyntaxError) as e:
        try:
            _recover(app)
        except (OSError, ValueError, SyntaxError):
            pass  # Keep journal and backup for the next launch if recovery cannot write.
        return False, "替换失败：%s" % str(e)[:160]


def confirm_update(app_dir):
    root = _root(app_dir)
    managed_path(root, JOURNAL).unlink(missing_ok=True)


def rollback(app_dir):
    try:
        root = _root(app_dir)
        with FileLock(managed_path(root, ".update.lock")):
            bak = managed_path(root, "app.bak")
            if not bak.exists():
                return False
            validate_app(bak)
            atomic_json(managed_path(root, JOURNAL), {"phase": "rollback"})
            _recover(app_dir)
            return True
    except (OSError, ValueError, SyntaxError, AlreadyRunning):
        return False


def mark_failed(app_dir, version):
    root = _root(app_dir)
    atomic_json(managed_path(root, QUARANTINE), {"version": version})


def _result(app_dir, reason, updated=False, **extra):
    return dict(updated=updated, version=read_local_version(app_dir), reason=reason, **extra)


def apply_update(app_dir, sources, log=print):
    app = Path(app_dir).absolute()
    failures = []
    try:
        root = _root(app)
        with FileLock(managed_path(root, ".update.lock")):
            _recover(app)
            local = read_local_version(app)
            current = version_key(local) if local else (0, 0, 0)
            candidates = []
            for base in sources or []:
                try:
                    man = _manifest(base)
                    candidates.append((base.rstrip("/"), man))
                except (OSError, ValueError, TypeError, UnicodeError, http.client.HTTPException) as e:
                    failures.append(str(e)[:120])
            if not candidates:
                log("[更新] 更新源不可达或未配置，继续使用现有版本")
                return _result(app, "no-source")
            candidates.sort(key=lambda pair: version_key(pair[1]["version"]), reverse=True)
            if version_key(candidates[0][1]["version"]) <= current:
                return _result(app, "same-version" if candidates[0][1]["version"] == local else "up-to-date")
            runtime = read_json(root / "runtime" / "manifest.json")
            quarantine = read_json(managed_path(root, QUARANTINE))
            for base, man in candidates:
                if version_key(man["version"]) <= current:
                    continue
                if quarantine.get("version") == man["version"]:
                    failures.append("该版本上次启动失败，等待后续版本")
                    continue
                if man.get("min_launcher", 1) > LAUNCHER_PROTOCOL or (runtime.get("dependency_lock_sha256") and runtime["dependency_lock_sha256"] != man.get("dependency_lock_sha256")):
                    failures.append("新版需要更新运行环境，请下载完整绿色包")
                    continue
                try:
                    path = managed_path(root, DOWNLOAD)
                    remove_managed(root, "app.new")
                    new = managed_path(root, "app.new")
                    new.mkdir()
                    if not download(base, man["zip"], path, url=man.get("url")):
                        raise OSError("下载失败或磁盘不可写")
                    good, why = verify(path, man)
                    if not good:
                        raise ValueError(why)
                    _safe_extract(path, new)
                    validate_app(new, man["version"], man.get("dependency_lock_sha256"))
                    good, why = swap_in(app, new)
                    if not good:
                        raise OSError(why)
                    log("[更新] 程序已替换，等待启动确认：%s" % man["version"])
                    return _result(app, "updated", True, source=base)
                except (OSError, ValueError, SyntaxError, zipfile.BadZipFile, RuntimeError) as e:
                    failures.append(str(e)[:160])
                    log("[更新] %s，保留旧版并尝试下一个源" % e)
                finally:
                    for name in (DOWNLOAD, "app.new"):
                        try:
                            remove_managed(root, name)
                        except (OSError, ValueError) as e:
                            log("[更新] 工作文件暂时无法清理：%s" % e)
            return _result(app, "update-failed", message="；".join(failures)[-400:])
    except (OSError, ValueError, SyntaxError, AlreadyRunning) as e:
        log("[更新] %s，继续使用现有版本" % e)
        return _result(app, "update-failed", message=str(e)[:200])


def write_status(data_dir, info):
    obj = dict(info)
    obj["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        atomic_json(Path(data_dir) / "update.status.json", obj)
        return True
    except OSError:
        return False

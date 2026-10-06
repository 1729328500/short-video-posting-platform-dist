"""Build only production app files; APP_VERSION is the single version source."""
import argparse
import hashlib
import json
import os
import sys
import uuid
import zipfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.common import atomic_json, is_link
from launcher.update import MAX_ARCHIVE, REQUIRED_FILES, parse_manifest, read_app_version, verify

GITEE_RELEASES = "https://gitee.com/guizhou-jubangbang_0/short-video-posting-platform-dist/releases/download"
FORBIDDEN = {"data", "profiles", "tests", "shots", "__pycache__", ".git", ".idea", "node_modules", "dist", "build"}
UI_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".ico", ".woff", ".woff2"}
VENDOR_SUFFIXES = UI_SUFFIXES | {".py", ".json", ".md", ".txt", ".yaml", ".yml"}
APP_FILES = {"server.py", "browser_channel.py", "browser_state.py", "data_worker.py", "dialog_settle.py",
             "login_qr_worker.py", "login_worker.py", "platform_login.py", "publish_worker.py",
             "sau_bridge.py", "platform_rules.json", "publish_adapters.json", "VERSION",
             "pyproject.toml", "requirements-desktop.lock"}


def production_files(app_dir):
    root = Path(app_dir).resolve()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        parts = rel.parts
        if any(part in FORBIDDEN or part.startswith(("_tmp", ".env")) for part in parts):
            continue
        if not p.is_file():
            continue
        if is_link(p) or not p.resolve().is_relative_to(root) or any(is_link(root.joinpath(*parts[:i])) for i in range(1, len(parts))):
            raise ValueError("生产文件不能是链接：%s" % rel)
        if len(parts) == 1:
            allowed = p.name in APP_FILES
        elif parts[0] == "ui":
            allowed = p.suffix.lower() in UI_SUFFIXES
        elif parts[0] == "tools":
            allowed = p.suffix == ".py"
        elif parts[0] == "vendor":
            allowed = p.suffix.lower() in VENDOR_SUFFIXES or p.name.upper().startswith(("LICENSE", "NOTICE"))
        else:
            allowed = False
        if allowed:
            yield p, rel.as_posix()


def _write_version(app_dir, version):
    p = Path(app_dir) / "VERSION"
    tmp = p.with_name(".VERSION-" + uuid.uuid4().hex)
    try:
        tmp.write_text(version + "\n", encoding="utf-8")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
    if p.read_text(encoding="utf-8").strip() != version:
        raise ValueError("VERSION 回读校验失败")


def build(app_dir, out_dir, release_base=GITEE_RELEASES):
    app_dir, out_dir = Path(app_dir).resolve(), Path(out_dir).resolve()
    version = read_app_version(app_dir)
    for name in REQUIRED_FILES:
        if name != "VERSION" and not (app_dir / name).is_file():
            raise ValueError("app 缺少关键文件：%s" % name)
    if out_dir == app_dir or out_dir.is_relative_to(app_dir):
        raise ValueError("打包输出必须在 app 目录以外")
    _write_version(app_dir, version)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / ("app-%s.zip" % version)
    tmp = out_dir / (".app-" + uuid.uuid4().hex + ".zip")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for p, name in production_files(app_dir):
                info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                z.writestr(info, p.read_bytes())
        if tmp.stat().st_size > MAX_ARCHIVE:
            raise ValueError("程序更新包超过大小限制")
        body = tmp.read_bytes()
        if final.exists() and hashlib.sha256(final.read_bytes()).digest() != hashlib.sha256(body).digest():
            raise ValueError("同版本安装包已存在但内容不同，请增加 APP_VERSION")
        os.replace(tmp, final)
        man = {"version": version, "zip": final.name, "size": len(body),
               "md5": hashlib.md5(body).hexdigest(), "sha256": hashlib.sha256(body).hexdigest(),
               "min_launcher": 1}
        if release_base:
            man["url"] = release_base.rstrip("/") + "/v%s/%s" % (version, final.name)
        lock = app_dir / "requirements-desktop.lock"
        if lock.exists():
            man["dependency_lock_sha256"] = hashlib.sha256(lock.read_bytes()).hexdigest()
        parse_manifest(json.dumps(man))
        atomic_json(out_dir / "version.json", man)
        back = json.loads((out_dir / "version.json").read_text(encoding="utf-8"))
        good, reason = verify(final, back)
        with zipfile.ZipFile(final) as z:
            if z.read("VERSION").decode("utf-8").strip() != back["version"]:
                raise ValueError("ZIP 内版本回读失败")
        if not good:
            raise ValueError("产物回读失败：%s" % reason)
        return dict(man, zip=final)
    finally:
        tmp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", nargs="?", default="app")
    parser.add_argument("output", nargs="?", default="_scratch/dist")
    parser.add_argument("--release-base", default=GITEE_RELEASES)
    args = parser.parse_args()
    result = build(args.app, args.output, args.release_base)
    print("打包完成：v%s %s (%d bytes)" % (result["version"], result["zip"], result["size"]))


if __name__ == "__main__":
    main()

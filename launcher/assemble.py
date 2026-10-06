"""Assemble a self-contained Windows x64 package from pinned installed dependencies."""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.common import atomic_json, is_link, remove_managed
from launcher.package import build
from launcher.start import DEFAULT_SOURCES, _environment

ROOT = Path(__file__).resolve().parent.parent


def dependencies(lock):
    pins = {}
    for line in Path(lock).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, version = line.split("==")
        dist = metadata.distribution(name)
        if dist.version != version:
            raise ValueError("依赖版本不匹配：%s，需要 %s，当前 %s" % (name, version, dist.version))
        pins[name] = dist
    if not {"playwright", "patchright", "requests"}.issubset(pins):
        raise ValueError("依赖锁缺少生产依赖")
    return pins


def required_browsers(pins):
    names = set()
    for name in ("playwright", "patchright"):
        obj = json.loads(pins[name].locate_file(name + "/driver/package/browsers.json").read_text())
        for b in obj["browsers"]:
            if b["name"] in ("chromium", "chromium-headless-shell", "ffmpeg", "winldd"):
                names.add(b["name"].replace("-", "_") + "-" + b["revision"])
    return sorted(names)


def copy_tree(source, destination, skip=()):
    source = Path(source).resolve()
    for p in source.rglob("*"):
        rel = p.relative_to(source)
        if any(part in set(skip) | {"__pycache__"} for part in rel.parts) or p.suffix == ".pyc":
            continue
        if is_link(p) or not p.resolve().is_relative_to(source):
            raise ValueError("运行环境中不允许链接：%s" % p)
        if p.is_file():
            dest = Path(destination) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)


def copy_python(home, target, pins):
    home, target = Path(home).resolve(), Path(target)
    if not (home / "python312.dll").is_file():
        raise ValueError("需要 Python 3.12 Windows x64 运行环境")
    target.mkdir(parents=True)
    for p in home.iterdir():
        if p.is_file() and (p.suffix in {".exe", ".dll"} or p.name == "LICENSE.txt"):
            shutil.copy2(p, target / p.name)
    copy_tree(home / "Lib", target / "Lib", skip=("site-packages", "test", "idlelib", "ensurepip", "turtledemo"))
    copy_tree(home / "DLLs", target / "DLLs")
    site = target / "Lib" / "site-packages"
    site.mkdir(parents=True)
    for name, dist in pins.items():
        if not dist.files:
            raise ValueError("依赖缺少安装文件清单：%s" % name)
        base = Path(dist.locate_file("")).resolve()
        for rel in dist.files:
            if any(part in {"..", "__pycache__", "test", "tests"} for part in rel.parts) or rel.suffix == ".pyc":
                continue  # Scripts entry points are unnecessary; use python -m instead.
            source = Path(dist.locate_file(rel))
            if is_link(source) or not source.resolve().is_relative_to(base):
                raise ValueError("依赖文件路径越界：%s" % rel)
            if source.is_file():
                destination = site / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    (target / "python312._pth").write_text(".\nDLLs\nLib\nLib/site-packages\n../..\n../../app\nimport site\n", encoding="utf-8")


def preflight(root, timeout=60):
    """Use ONLY the packaged interpreter and browsers, with no user site or PATH help."""
    root = Path(root).resolve()
    python = root / "runtime/python/python.exe"
    env = _environment(root, "preflight")
    env["PATH"] = str(root / "runtime/python") + os.pathsep + os.path.join(os.environ.get("SystemRoot", "C:/Windows"), "System32")
    script = '''import sys,struct,importlib.metadata as m,json,importlib
assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8
import server,requests
assert hasattr(server.sau_bridge,'PLATFORMS')
sys.path.insert(0,str(server.APP_DIR/'vendor/sau'))
for value in server.sau_bridge.PLATFORMS.values():
    assert hasattr(importlib.import_module(value['module']),value['cls'])
for package in ('playwright','patchright'):
    module=__import__(package+'.sync_api',fromlist=['sync_playwright'])
    factory=getattr(module,'sync_playwright',None) or module.sync_playwright
    with factory() as p:
        browser=p.chromium.launch(headless=True,channel='chromium')
        page=browser.new_page();page.set_content('<title>packaged browser</title>')
        assert page.title()=='packaged browser'
        browser.close()
print('runtime and browsers OK')
'''
    result = subprocess.run([str(python), "-X", "utf8", "-c", script], cwd=root / "app", env=env,
                            capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    if result.returncode:
        raise ValueError("绿色包运行环境自检失败：\n" + (result.stdout + result.stderr)[-3000:])
    return result.stdout.strip()


def assemble(output, browser_cache, python_home=None, source=ROOT, archive=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or source.is_relative_to(output) or output.is_relative_to(source / "app"):
        raise ValueError("输出不能覆盖源码或放在 app 内")
    if output.exists():
        raise ValueError("输出目录已存在，请使用新的目录；不覆盖已有绿色包")
    lock = source / "app/requirements-desktop.lock"
    pins = dependencies(lock)
    browser_cache = Path(browser_cache).resolve()
    needed = required_browsers(pins)
    for name in needed:
        if not (browser_cache / name).is_dir():
            raise ValueError("浏览器版本缺失：%s；请用锁定版本的 python -m playwright install chromium 安装" % name)
    output.mkdir(parents=True)
    # No cleanup on error: leave the bounded output for diagnostics, never remove arbitrary paths.
    (output / "app").mkdir()
    manifest = build(source / "app", output / "_build", release_base="")
    with zipfile.ZipFile(manifest["zip"]) as z:
        z.extractall(output / "app")  # Locally generated production whitelist, never remote input.
    remove_managed(output, "_build")
    copy_tree(source / "launcher", output / "launcher")
    for name in ("Start.bat", "Install.bat", "启动工具.bat", "安装.bat"):
        shutil.copy2(source / "launcher" / name, output / name)
    copy_python(python_home or sys.base_prefix, output / "runtime/python", pins)
    for name in needed:
        copy_tree(browser_cache / name, output / "browsers" / name)
    atomic_json(output / "runtime/manifest.json", {
        "python": "3.12", "architecture": "x64", "dependencies": {k: v.version for k, v in pins.items()},
        "browsers": needed, "dependency_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest()})
    atomic_json(output / "install.json", {"port": 8947, "update_sources": list(DEFAULT_SOURCES)})
    shutil.copy2(source / "launcher/README.md", output / "README.md")
    preflight(output)
    if archive:
        shutil.make_archive(str(output), "zip", root_dir=output.parent, base_dir=output.name)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output")
    parser.add_argument("--browsers", required=True)
    parser.add_argument("--python-home")
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    try:
        print("绿色包完成：%s" % assemble(args.output, args.browsers, args.python_home, archive=args.archive))
        return 0
    except (OSError, ValueError, metadata.PackageNotFoundError, subprocess.SubprocessError) as e:
        print("组装失败：%s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

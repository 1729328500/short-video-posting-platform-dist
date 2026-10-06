"""Small standard-library helpers shared by desktop components."""
import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path


def read_json(path, default=None):
    try:
        p = Path(path)
        if p.stat().st_size > 65536:
            raise ValueError("JSON 文件过大")
        value = json.loads(p.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError, UnicodeError):
        return default or {}


def atomic_json(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def is_link(path):
    p = Path(path)
    return p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())


def managed_path(root, name):
    """Accept only a fixed direct child, never a path supplied by a manifest."""
    root = Path(root).resolve()
    if Path(name).name != name or name in ("", ".", ".."):
        raise ValueError("非法工作路径")
    p = root / name
    if is_link(p) or p.resolve().parent != root:
        raise ValueError("拒绝操作链接或包目录以外的路径：%s" % p)
    return p


def remove_managed(root, name):
    p = managed_path(root, name)  # Check the final absolute target before rmtree.
    if p.is_dir():
        if (p / "data").exists():
            raise ValueError("工作目录含有数据，保留原目录：%s" % p)
        shutil.rmtree(p)
    else:
        p.unlink(missing_ok=True)


def data_id(path):
    value = os.path.normcase(str(Path(path).resolve()))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_data_path(path, package_root):
    p = Path(path).expanduser().resolve()
    root = Path(package_root).resolve()
    if p == root:
        raise ValueError("数据目录不能是程序包根目录")
    for name in ("app", "app.new", "app.bak", "app.failed", "runtime", "browsers", "launcher"):
        forbidden = root / name
        if p == forbidden or p.is_relative_to(forbidden) or forbidden.is_relative_to(p):
            raise ValueError("数据目录不能与程序或更新工作目录重叠：%s" % p)
    return p

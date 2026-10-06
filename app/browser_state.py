"""Keep session cookies across clean browser shutdowns in the private data folder.

Chromium's profile retains persistent cookies, localStorage and IndexedDB. A
separate snapshot restores missing session cookies without
replacing newer tokens already in that profile. Each normal close replaces the
snapshot, including an empty state after logout.
"""
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


def state_path(profile):
    profile = Path(profile)
    return profile.with_name(profile.name + ".storage.json")


def save(ctx, profile):
    state = ctx.storage_state(indexed_db=True)
    path = state_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as f:
            temporary = Path(f.name)
            os.chmod(temporary, 0o600)
            json.dump({"version": 1, "state": state}, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return state


def restore(ctx, profile):
    path = state_path(profile)
    if not path.exists():
        return
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("version") != 1:
            raise ValueError("unsupported state version")
        state = record["state"]
        def cookie_key(cookie):
            return (cookie["name"], cookie["domain"], cookie["path"],
                    json.dumps(cookie.get("partitionKey"), sort_keys=True))
        live = {cookie_key(c) for c in ctx.cookies()}
        # Expiring cookies belong to the native profile; do not resurrect deleted
        # or expired persistent tokens from an older snapshot.
        missing = [c for c in state.get("cookies", [])
                   if c.get("expires") == -1 and cookie_key(c) not in live]
        if missing:
            ctx.add_cookies(missing)
    except Exception:
        # A broken backup must never remove the usable native browser profile.
        print("[browser] 登录态备份无法恢复，继续使用原浏览器数据", file=sys.stderr, flush=True)


def close(ctx, profile, required=False):
    try:
        save(ctx, profile)
    except Exception as exc:
        if required:
            raise OSError("保存登录态失败，请检查数据目录后重试") from exc
        print("[browser] 登录态备份保存失败，继续关闭浏览器", file=sys.stderr, flush=True)
    ctx.close()


@contextmanager
def persistent_context(browser_type, *, user_data_dir, **kwargs):
    ctx = browser_type.launch_persistent_context(user_data_dir=str(user_data_dir), **kwargs)
    closed = False
    def on_close(*_):
        nonlocal closed
        closed = True
    ctx.on("close", on_close)
    try:
        restore(ctx, user_data_dir)
        yield ctx
    finally:
        if not closed:
            close(ctx, user_data_dir)

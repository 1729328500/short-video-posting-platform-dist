"""OS-held locks: a crash releases ownership without a stale PID timeout."""
import errno
import os
from pathlib import Path

from launcher.common import is_link


class AlreadyRunning(RuntimeError):
    pass


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def acquire(self):
        if self.file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if is_link(self.path):
            raise ValueError("锁文件不能是链接")
        f = self.path.open("a+b")
        try:
            if f.seek(0, os.SEEK_END) == 0:
                f.write(b"0")
                f.flush()
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            f.close()
            if e.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return False
            raise
        self.file = f
        return True

    def release(self):
        if self.file is None:
            return
        f, self.file = self.file, None
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()

    def __enter__(self):
        if not self.acquire():
            raise AlreadyRunning("已有实例正在使用：%s" % self.path.parent)
        return self

    def __exit__(self, *args):
        self.release()

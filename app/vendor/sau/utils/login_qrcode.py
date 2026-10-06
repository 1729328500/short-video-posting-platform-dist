# -*- coding: utf-8 -*-
"""二维码辅助——本项目自建替身。

原版依赖 cv2 + numpy + segno（三个都不小），只为了「解二维码图片 / 在终端画画」。
**本项目不使它们的扫码登录流程**：登录仍然走我们自己的持久化 profile + 登录窗
（见 app/login_worker.py），所以这里
  · 保留上传器在 import 期就会引用到的函数名（否则模块导入就崩）
  · 把「确实需要 QR 解码/绘制」的两个函数做成显式报错，而不是静默返回错值

如果哪天真的需要它们的二维码登录，把原版 login_qrcode.py 覆盖回来并装
cv2/numpy/segno 三个依赖即可。
"""
import base64
import re
from datetime import datetime
from pathlib import Path


def build_login_qrcode_path(account_file: str, suffix: str = "login_qrcode") -> Path:
    account_path = Path(account_file)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return account_path.with_name(f"{account_path.stem}_{suffix}_{timestamp}.png")


def save_data_url_image(data_url: str, output_path: Path) -> Path:
    """把 data:image/...;base64,xxx 存成文件（不需要 cv2）"""
    if not data_url.startswith("data:image/"):
        raise ValueError("二维码地址不是 data:image 格式")
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("二维码地址不是 base64 编码")
    raw = base64.b64decode(re.sub(r"\s+", "", encoded))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(raw)
    return output_path


def remove_qrcode_file(qrcode_path: Path | None) -> bool:
    if qrcode_path and Path(qrcode_path).exists():
        try:
            Path(qrcode_path).unlink()
            return True
        except OSError:
            return False
    return False


def decode_qrcode_from_path(qrcode_path: Path) -> str | None:
    """解出二维码内容——**本替身不做**，返回 None。

    ★ 这里曾经是 `raise RuntimeError(...)`，结果把整个扫码登录搞挂了（实测踩过）：
      上游 `_save_douyin_qrcode()` 在存完二维码之后会调这个函数，本意只是
      「顺便在终端画一个字符版二维码」；返回 None 时上游已经有兜底分支
      （只警告「终端没法完整显示，请打开文件扫码」），照常继续登录。
      抛异常却会向上冒泡，把一次本来成功的登录判成失败。
      ⇒ 我们的形态是「把二维码交给网页显示」，根本不需要终端解码，所以安静返回 None 即可。
    """
    return None


def print_terminal_qrcode(qrcode_content: str, qrcode_path: Path,
                          app_name: str, compact: bool = True, border: int = 1) -> None:
    print("请使用%s扫描二维码登录（二维码文件：%s）" % (app_name, qrcode_path))

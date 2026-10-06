# -*- coding: utf-8 -*-
"""扫码登录（无头 · 二维码交给调用方）—— 服务器部署形态的登录入口。

★ 与 login_worker.py 的区别：
  login_worker 是「在本机弹一个有头浏览器窗口，用户看着窗口扫」—— 只适合本机。
  本工具是「无头跑，把二维码交给调用方」—— 调用方可以是网页（把二维码画在页面上），
  于是**用户在哪儿都能扫**，这是部署到服务器后的正确形态。

★ 二维码怎么交出去：SAU 的 qrcode_callback 会收到
      {"image_path": "...", "image_data_url": "data:image/png;base64,..."}
  image_data_url 直接塞进 <img src> 就能显示。

用法：
    python app/tools/qr_login.py --state <状态文件> --qr-out <二维码png> [--headless 0/1]
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
VENDOR = APP / "vendor" / "sau"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True, help="登录态 storage_state JSON 的输出路径")
    ap.add_argument("--qr-out", required=True, help="二维码 PNG 的落盘路径")
    ap.add_argument("--data-dir", default=str(APP / "data"))
    ap.add_argument("--key", default="douyin", help="目前仅 douyin")
    ap.add_argument("--headless", type=int, default=1, help="1=无头（服务器形态）；0=有头")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--max-checks", type=int, default=200,
                    help="轮询次数；×3 秒＝总等待窗口。★ 默认值别调小 —— "
                         "SAU 内部默认只等 120 秒，对真人扫码太紧（实测踩过）")
    args = ap.parse_args()

    if args.key != "douyin":
        print(json.dumps({"ok": False, "error": "目前只实现了 douyin 的二维码登录"},
                         ensure_ascii=False))
        return 2

    # SAU 需要的路径环境
    sau_base = Path(args.data_dir) / "sau"
    sau_base.mkdir(parents=True, exist_ok=True)
    os.environ["SAU_BASE_DIR"] = str(sau_base)
    os.environ["SAU_LOG_FILE"] = str(sau_base / "qr_login.log")
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))

    import asyncio

    got = {"at": 0.0}

    def save_qr(payload):
        """SAU 的回调：拿到二维码就落盘。刷新时会再次回调。"""
        url = (payload or {}).get("image_data_url") or ""
        if not url.startswith("data:image/"):
            return
        try:
            _, encoded = url.split(",", 1)
            raw = base64.b64decode(re.sub(r"\s+", "", encoded))
            Path(args.qr_out).write_bytes(raw)
            got["at"] = time.time()
            print("QR_READY %s %d bytes" % (args.qr_out, len(raw)), flush=True)
        except Exception as e:  # noqa: BLE001
            print("QR_SAVE_FAILED %s" % str(e)[:120], flush=True)

    async def run():
        from uploader.douyin_uploader.main import cookie_auth, douyin_cookie_gen
        # ★ 不走 douyin_setup：它内部把 max_checks 写死成 60（×2 秒＝120 秒），
        #   真人扫码经常来不及（实测：用户还没扫上就超时了）。
        #   这里直接调 douyin_cookie_gen，把窗口放到 --max-checks × 3 秒。
        if await cookie_auth(args.state):
            return {"success": True, "status": "cookie_valid", "message": "该登录态仍然有效"}
        return await douyin_cookie_gen(
            args.state,
            qrcode_callback=save_qr,
            poll_interval=3,
            max_checks=args.max_checks,
            headless=bool(args.headless),
        )

    t0 = time.time()
    try:
        res = asyncio.run(asyncio.wait_for(run(), timeout=args.timeout))
    except asyncio.TimeoutError:
        res = {"success": False, "status": "timeout", "message": "等待扫码超时"}
    except Exception as e:  # noqa: BLE001
        res = {"success": False, "status": "error", "message": str(e)[:200]}

    out = {
        "ok": bool(res.get("success")),
        "status": res.get("status"),
        "message": (res.get("message") or "")[:200],
        "state_file": args.state,
        "qr_out": args.qr_out,
        "qr_seen": bool(got["at"]),
        "headless": bool(args.headless),
        "seconds": round(time.time() - t0, 1),
    }
    # 登录成功的话确认一下 sessionid 在不在
    try:
        d = json.loads(Path(args.state).read_text(encoding="utf-8"))
        names = [c.get("name") for c in (d.get("cookies") or [])]
        out["cookies"] = len(names)
        out["has_sessionid"] = "sessionid" in names
    except Exception:
        out["has_sessionid"] = False
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

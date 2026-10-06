from pathlib import Path
from typing import List

from conf import BASE_DIR

SOCIAL_MEDIA_DOUYIN = "douyin"
SOCIAL_MEDIA_TENCENT = "tencent"
SOCIAL_MEDIA_TIKTOK = "tiktok"
SOCIAL_MEDIA_BILIBILI = "bilibili"
SOCIAL_MEDIA_KUAISHOU = "kuaishou"


def get_supported_social_media() -> List[str]:
    return [SOCIAL_MEDIA_DOUYIN, SOCIAL_MEDIA_TENCENT, SOCIAL_MEDIA_TIKTOK, SOCIAL_MEDIA_KUAISHOU]


def get_cli_action() -> List[str]:
    return ["upload", "login", "watch"]


async def set_init_script(context):
    # ★ 本项目改动（原版是 Path(BASE_DIR / "utils/stealth.min.js")）：
    #   改成按【本模块所在位置】找 stealth.min.js。原写法把 BASE_DIR 钉死在代码目录，
    #   导致 cookies/verify_code.txt 等运行期产物也只能落在源码树里。
    #   解耦之后 BASE_DIR 可以指向数据目录（app/data/sau），源码树保持干净。
    stealth_js_path = Path(__file__).resolve().parent / "stealth.min.js"
    await context.add_init_script(path=stealth_js_path)
    return context

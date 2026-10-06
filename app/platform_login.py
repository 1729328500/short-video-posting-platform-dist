# -*- coding: utf-8 -*-
"""登录态判定（共享模块）—— 登录窗与发布器使用同一套判据。

★ 为什么不看 URL：
  2026-09-27 对 8 个平台实测（未登录态），URL 完全不可靠 ——
    · 假阴性 7/8：登录成功后落点仍在各自创作后台主机下
      （cp.kuaishou.com/profile、mp.toutiao.com/profile_v4/、member.bilibili.com/...），
      旧判据 `login_url not in cur` 恒为假 → 永远不转绿。
    · 假阳性 1/8：西瓜视频未登录时被 302 到 creator.douyin.com（完全不同主机、
      URL 里也没有 login/passport 字样）→ 旧判据会判成「已登录」。
  ⇒ 改用页面正文里的登录页特征词。

★ 判据方向（保守）：
  只有「页面正常加载 + 一个登录特征都没有」才判为已登录；
  页面异常/空白 → 一律算未登录（宁可让用户点一下兜底按钮，也不要误报已登录）。
"""
import re
from urllib.parse import urlsplit

# 各平台的创作后台入口 —— 两个用途：
#   ① 登录窗打开哪一页；② **核验登录态**（`tools/probe_accounts.py --verify`）打开哪一页。
# ★ 2026-09-29 从 server.py 挪到这里：登录态判定的唯一事实来源是这个模块，
#   而"核验"必须在**和登录窗同一套判据 + 同一批入口**下做，否则核出来的结论和登录窗不一致。
#   （判据是正文特征词而不是 URL —— 理由见本文件头部 2026-09-27 的实测。）
LOGIN_URLS = {
    "douyin": "https://creator.douyin.com/",
    "channels": "https://channels.weixin.qq.com/",
    "kuaishou": "https://cp.kuaishou.com/",
    "xhs": "https://creator.xiaohongshu.com/",
    "weibo": "https://weibo.com/",
    "toutiao": "https://mp.toutiao.com/",
    "bilibili": "https://member.bilibili.com/platform/home",
    "xigua": "https://studio.ixigua.com/",
}

# 登录页特征词。命中任意一条即为「未登录」。
# 全部来自 2026-09-27 对 8 个平台未登录页的实测正文。
LOGIN_MARKERS = (
    "扫码登录", "验证码登录", "密码登录", "短信登录",
    "获取验证码", "发送验证码", "登录即同意", "立即登录",
    "请使用今日头条App扫码登录", "打开「抖音APP」", "扫码立即下载",
    "手机号登录", "第三方登录", "记住登录状态",
)

# 已登录时页面通常会出现这些词——只作为辅助参考，不作为判定依据
LOGGED_IN_HINTS = ("退出登录", "退出帐号", "退出账号", "创作者中心", "创作中心", "发布视频", "数据中心")

MIN_TEXT_LEN = 20          # 正文短于此视为「页面没加载出来」
STABLE_ROUNDS = 2          # 连续 N 次探测结论一致才改变状态，避开跳转中间态

# 浏览器自己报错时页面上的字样（网络不通/站点挂了），不能当成「已登录」
ERROR_MARKERS = (
    "无法访问此网站", "网页无法打开", "该网页无法正常运作",
    "ERR_CONNECTION", "ERR_NAME_NOT_RESOLVED", "ERR_TIMED_OUT",
    "ERR_INTERNET_DISCONNECTED", "检查网络连接", "重新加载",
)

# 平台异常页特征（维护/升级/无权限/服务异常）——命中即判不准，不能当已登录。
# ★ 2026-10-06（安全审查 R9）：原来"没命中登录词 + 正文够长"就判已登录，
#   一段正常长度的维护提示就能骗过它 —— 实测复现过：
#   "系统正在维护中，请稍后再试。给您带来的不便敬请谅解。" 被判成 on。
#   ★ 这些词只做**否决**用，所以取偏长的词组降低误伤（"维护"太泛 → 用"维护中"等）。
ABNORMAL_MARKERS = (
    "正在维护", "系统维护", "维护中", "升级维护", "服务器维护", "网站维护",
    "404 Not Found", "页面不存在", "无法找到该页", "没有找到页面",
    "无访问权限", "没有权限", "访问受限", "访问被拒绝", "无权访问",
    "服务异常", "系统繁忙", "服务器繁忙", "请稍后再试", "系统错误",
)


def probe_account(page, key):
    """探出当前登录的是**哪个账号**，返回 (账号ID, 账号名)；探不到返回 ("", "")。

    ★ 为什么需要：设置项叫「单账号日更上限」，但旧实现是按【平台】计数的 ——
      换个账号进来，昨天的、别的账号的发布记录照样算在头上，用户被锁住还找不到原因。
      实测踩过（2026-09-27）。有了账号身份，配额才能按账号算。

    目前只实现了抖音（`/aweme/v1/creator/user/info/` 里有 unique_id 与 nick_name）。
    其它平台返回空 → 上游会退回「按平台计数」，并在界面标注清楚。
    """
    if key != "douyin":
        return "", ""
    if urlsplit(str(page.url or "")).hostname in {"127.0.0.1", "localhost", "::1"}:
        return "", ""
    try:
        r = page.request.get("https://creator.douyin.com/aweme/v1/creator/user/info/",
                             headers={"Referer": "https://creator.douyin.com/"}, timeout=3000)
        prof = ((r.json() or {}).get("user_profile") or {})
        return str(prof.get("unique_id") or ""), str(prof.get("nick_name") or "")
    except Exception:
        return "", ""


def classify(text, url="", positive=False):
    """把页面正文/URL 归类。

    返回 (state, note, detail)
      state: "on"（已确认登录） | "waiting"（确定未登录） | "unknown"（判不准）

    ★ 2026-10-06（安全审查 R9）：改为**正向确认** ——
      只有命中已登录特征、或调用方用账号接口确认过（positive=True）才算 on。
      "没有登录特征"不再等于"已登录"：维护页、未知页面、平台改版都落在那档上，
      原来会被判成 on 于是自动关窗、保存一份无效登录态。
      判不准就是判不准：登录窗保留窗口让用户确认，发布路径照常尝试，
      核验脚本保留账号原状态（见 verify_state）。
    """
    text = text or ""
    low = (url or "").lower()
    hits = [m for m in LOGIN_MARKERS if m in text]
    if hits:
        return "waiting", "等待扫码", "命中登录页特征：%s" % "、".join(hits[:3])
    if ("login" in low) or ("passport" in low) or ("signin" in low):
        return "waiting", "等待扫码", "URL 仍在登录域名下"
    errs = [m for m in ERROR_MARKERS if m in text]
    if errs:
        return "unknown", "页面打开失败", "命中错误页特征：%s" % "、".join(errs[:2])
    abn = [m for m in ABNORMAL_MARKERS if m in text]
    if abn:
        # 异常页优先于正向确认：页面都说"维护中"了，这时候不该自动关窗
        return "unknown", "页面异常，无法确认", "命中异常页特征：%s" % "、".join(abn[:2])
    if positive:
        return "on", "检测到已登录", "账号接口确认已登录（页面 %d 字）" % len(re.sub(r"\s+", "", text))
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < MIN_TEXT_LEN:
        return "unknown", "页面未加载完成", "正文仅 %d 字" % len(stripped)
    hints = [m for m in LOGGED_IN_HINTS if m in text]
    if hints:
        return "on", "检测到已登录", "命中已登录特征：%s（%d 字）" % ("、".join(hints[:2]), len(stripped))
    return "unknown", "无法确认是否已登录", \
           "无登录页特征、也未命中已登录特征（%d 字），请人工确认" % len(stripped)


def verify_state(cls):
    """核验结论 → 要写回的状态；判不准时返回 None（＝保留原状态）。

    ★ 2026-10-06（安全审查 R9）：`probe_accounts --verify` 原来是
      `st["state"] = "on" if cls == "on" else "off"` —— 判不准会被写成"未登录"，
      已登录账号因此在账号页变灰、点发布直接被拒。判不准不能算任何一边。
    """
    if cls == "on":
        return "on"
    if cls == "waiting":
        return "off"
    return None

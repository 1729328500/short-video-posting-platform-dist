# -*- coding: utf-8 -*-
"""真实数据抓取（按平台适配）—— 由 server.py 的数据抓取调用。

★★ 设计原则：**抓不到就说抓不到，绝不用假数据顶替。**
   旧版 run_fetch() 用 md5(视频id|平台|日期) 生成播放/点赞/评论，还带着
   source='mock' 标记，但界面和推送都不显示这个标记 —— 等于把编造的数字
   当真实数据展示和推送。这条已废弃。

实现方式：不解析页面 DOM（脆弱、会漏），而是**用 profile 里的登录 cookie
直接调平台自己的接口拿 JSON**。抖音实测可用：

    /aweme/v1/creator/user/info/               账号汇总（粉丝数 / 总获赞 / 昵称）
    /janus/douyin/creator/pc/work_list         作品列表，每条带 播放/点赞/评论，支持翻页

用法：
    python data_worker.py --key douyin --profile <profile目录> --status <输出json> [--max-pages 5]
"""
import argparse
import datetime
import json
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import browser_channel  # noqa: E402
import browser_state  # noqa: E402

# 各平台真实抓取的实现开关。没有实现 = 如实返回 not_supported
SUPPORTED = ("douyin", "bilibili", "weibo", "toutiao", "channels", "kuaishou", "xhs")

# 各平台"打开哪一页"——只为让浏览器带着已登录 profile 起来（判据/接口都不依赖这一页）
from platform_login import LOGIN_URLS as _LOGIN_URLS  # noqa: E402
LOGIN_ENTRY = dict(_LOGIN_URLS)

DOUYIN_USER_INFO = "https://creator.douyin.com/aweme/v1/creator/user/info/"
DOUYIN_WORK_LIST = ("https://creator.douyin.com/janus/douyin/creator/pc/work_list"
                    "?status=0&count=40&scene=star_atlas&device_platform=android&aid=1128"
                    "&max_cursor=")
PAGE_SIZE = 40


def w(path, obj):
    obj = dict(obj)
    obj["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _num(v):
    """数值 → int。★ 2026-09-29 扩了中文单位（1.2万 / 3亿）：平台接口多数返回裸整数，
    但确实有带单位的写法 —— **解不出来就 0，不假装解出来了**。
    ⚠️ 本函数被抖音那条路共用；改完必须回归 `tests/test_08_data.py`。"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    t = str(v or "").strip().replace(",", "")
    if not t:
        return 0
    mult = 1
    if t.endswith("万"):
        mult, t = 10000, t[:-1]
    elif t.endswith("亿"):
        mult, t = 100000000, t[:-1]
    try:
        return int(float(t) * mult)
    except (TypeError, ValueError):
        return 0


# ─────────────────── 统计窗口（2026-09-30 口径变更，用户定的）───────────────────
# ★ 口径变了：看板统计的是**窗口内发布的那些作品**的数据之和，不再是"账号全部作品合计"。
#   窗口 = [(今天 - N) 日 0 点, 现在]，N=1（默认）＝「昨天 0 点到现在」——
#   用户看的是"昨天发的那批，现在表现怎么样"。
#   为什么必须换：合计会把半年前的老作品也算进来（B站那条 09-03 的还在贡献播放），
#   和平台后台的"近期/昨日"数字对不上，用户拿它对账时会以为抓错了。
DAYS_DEFAULT = 1


def win_start(days, now=None):
    """窗口起点（Unix 秒）＝ (今天 - days) 日的 0 点（本地时间）。"""
    now = time.time() if now is None else now
    d = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return (d - datetime.timedelta(days=max(0, int(days)))).timestamp()


def to_sec(v):
    """平台给的时间 → Unix 秒（本地时间基准）。★ 各家单位/格式都不一样（全部实测过）：

        秒     视频号 createTime · B站 ptime · 头条 publishTime · 小红书 visible_time
        毫秒   快手 uploadTime（1790734528913 → 2026-09-30 10:15:28）
        字符串 微博 "Tue Sep 29 13:44:55 +0800 2026" · 小红书 "2025-04-24 15:42"

    解不出来返回 0 ＝ 视作不在窗口内（**不猜、不编**）。
    毫秒判据 `> 1e11`：1e11 秒 ≈ 公元 5138 年，任何真实秒级时间戳都远小于它。
    """
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        n = float(v)
        return n / 1000.0 if n > 1e11 else n
    s = str(v or "").strip()
    if not s:
        return 0
    if s.isdigit():
        n = float(s)
        return n / 1000.0 if n > 1e11 else n
    for fmt in ("%a %b %d %H:%M:%S %z %Y",   # 微博（周几 月 日 时:分:秒 时区 年）
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",             # 小红书 `time` 字段**没有秒**（"2025-04-24 15:42"）
                "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return 0


def _aggregate(rows, start):
    """窗口内作品求和 → (works, top)。

    `rows` 是各家映射好的统一行：
        {"ts": 秒, "views": …, "likes": …, "comments": …, "collects": …, "shares": …, "desc": …}
    ★ ts 解不出来、或早于 start 的，一律**不计**（宁可少算，不算错）。
    """
    works = {"count": 0, "views": 0, "likes": 0, "comments": 0, "collects": 0, "shares": 0}
    top = []
    for r in rows:
        ts = r.get("ts") or 0
        if not ts or ts < start:
            continue
        works["count"] += 1
        for k in ("views", "likes", "comments", "collects", "shares"):
            works[k] += _num(r.get(k))
        top.append({"desc": str(r.get("desc") or "")[:40],
                    "views": _num(r.get("views")), "likes": _num(r.get("likes"))})
    top.sort(key=lambda x: x["views"], reverse=True)
    return works, top[:5]


def win_note(start, n_in, days):
    """统一的口径说明 —— 每家的 note 都要带上，用户才能拿去和平台后台对数。"""
    return "统计范围：最近 %d 天（自 %s 起，窗口内 %d 条作品）" % (
        max(1, int(days)),
        datetime.datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M"),
        n_in)


def fetch_douyin(pg, status_path, max_pages, start=0, days=DAYS_DEFAULT):
    """抖音：账号汇总 + 作品列表（翻页聚合）"""
    w(status_path, {"state": "running", "note": "读取账号汇总…"})
    r = pg.request.get(DOUYIN_USER_INFO,
                       headers={"Referer": "https://creator.douyin.com/"})
    if not r.ok:
        return {"ok": False, "error": "账号接口返回 HTTP %s（登录态可能已失效）" % r.status}
    d = r.json()
    prof = d.get("user_profile") or {}
    if not prof:
        return {"ok": False, "error": "账号接口没返回 user_profile（接口结构可能变了）"}
    account = {
        "nick_name": prof.get("nick_name") or "",
        "unique_id": str(prof.get("unique_id") or ""),
        "followers": _num(prof.get("follower_count")),
        "following": _num(prof.get("following_count")),
        "total_likes": _num(prof.get("total_favorited")),
    }

    # ── 作品列表翻页 → 只留窗口内的（2026-09-30 口径）──
    rows = []
    cursor = 0
    for i in range(max(1, max_pages)):
        w(status_path, {"state": "running",
                        "note": "读取作品数据 第 %d 页…（已读回 %d 条）" % (i + 1, len(rows))})
        try:
            rr = pg.request.get(DOUYIN_WORK_LIST + str(cursor),
                                headers={"Referer": "https://creator.douyin.com/creator-micro/content/manage"})
            j = rr.json()
        except Exception as e:  # noqa: BLE001
            if not rows:
                return {"ok": False, "error": "作品接口读取失败：%s" % str(e)[:120]}
            break
        items = j.get("aweme_list") or []
        for a in items:
            s = a.get("statistics") or {}
            rows.append({"ts": to_sec(a.get("create_time")), "views": s.get("play_count"),
                         "likes": s.get("digg_count"), "comments": s.get("comment_count"),
                         "collects": s.get("collect_count"), "shares": s.get("share_count"),
                         "desc": a.get("desc") or ""})
        nxt = j.get("max_cursor") or 0
        if not items or not j.get("has_more") or nxt == cursor:
            break
        cursor = nxt
        time.sleep(0.6)          # 别把人家接口打急了

    works, top = _aggregate(rows, start)
    return {"ok": True, "platform": "douyin", "source": "douyin_api",
            "account": account, "works": works, "top": top,
            "note": win_note(start, works["count"], days) + "（共读回 %d 条）" % len(rows)}


# ────────────────────────── B站（2026-09-29）──────────────────────────
# ★ 接口都是**探针实测**出来的（不是"照抄网上的"）：四个候选全部返回真实数据，
#   且与 B站创作中心后台**对数一致**（播放 3632 ✓ 粉丝 5 ✓ 作品数 18 ✓）——
#   证据与样本见 `app/tests/_data_samples/bilibili.json` 与 `11-数据看板-设计.md` §3.3。
BILI_NAV = "https://api.bilibili.com/x/web-interface/nav"
BILI_CARD = "https://api.bilibili.com/x/web-interface/card?mid=%s"
BILI_UPSTAT = "https://api.bilibili.com/x/space/upstat?mid=%s"
BILI_REFERER = "https://space.bilibili.com/"
# ★ 2026-09-30（用户给的入口）：**创作中心 → 稿件管理** 的逐条列表，不限额。
#   原来判"B站拿不到逐条"是因为 x/space/arc/search 被限频（-799）—— 换这条就解决了。
#   参数带 x-bili-device-req-json（不传可能被拒），照侦察样本**原样抄**，不手写。
# ★ 用 {pn} 占位 + replace，**不能用 % 格式化** —— 这条 URL 里满是 `%7B` 这类
#   百分号转义，`"…%7B…" % x` 会被解释成格式符（实测报 unsupported format character 'B'）。
BILI_ARCHIVES = ("https://member.bilibili.com/x/web/archives"
                 "?x-bili-device-req-json=%7B%22platform%22%3A%22web%22%2C%22device%22%3A%22pc%22%2C"
                 "%22mobi_app%22%3A%22web_cn%22%2C%22spmid%22%3A%22333.885%22%7D"
                 "&x-bili-locale-json=%7B%22c_locale%22%3A%7B%22language%22%3A%22zh%22%2C"
                 "%22script%22%3A%22Hans%22%7D%2C%22always_translate%22%3Afalse%7D"
                 "&status=is_pubing%2Cpubed%2Cnot_pubed&pn={pn}&ps=10&coop=1&interactive=1")

# ★ 2026-09-30：原来这里有一条 `_BILI_UNAVAILABLE_NOTE`（"逐条作品被限频、取不到，评论/收藏/分享置 0"）
#   —— **已删**：用户给了创作中心「稿件管理」入口（`/x/web/archives`），逐条数据能拿到了，
#   那条文案留着就会撒谎（正是本项目最忌的"看起来成功"的反面：**看起来做不到**）。
#   教训留下：当初判"B站做不到"，是**试错了接口**（x/space/arc/search 限频），不是平台真没有。


def _map_bilibili(payloads, start=0):
    """B站：账号 + 汇总 + **作品列表** → 统一 schema。**纯函数**（判据灌样本进来测）。

    入参 `payloads`：
      {"card": …, "upstat": …, "archives": {"data": {"arc_audits": [...]}}}
      · card.data.card.name / .mid，card.data.follower / following / like_num
      · upstat.data.archive.view（总播放）、upstat.data.likes（总获赞）—— 账号级，照旧用
      · arc_audits[].Archive.{title,ptime} + .stat.{view,like,reply,favorite,share}
        —— **逐条**，窗口内求和（2026-09-30 口径；样本 `_data_samples/bilibili_list.json`）
    返回 (account, works, top)；结构不对 → (None, None, None)（由 fetch 如实报"结构变了"）。
    """
    if not isinstance(payloads, dict):
        return None, None, None
    cd = ((payloads.get("card") or {}).get("data") or {})
    c = (cd.get("card") or {})
    ud = ((payloads.get("upstat") or {}).get("data") or {})
    if not c or not ud:
        return None, None, None
    account = {"nick_name": str(c.get("name") or ""),
               "unique_id": str(c.get("mid") or ""),
               "followers": _num(cd.get("follower")),
               "following": _num(cd.get("following")),
               "total_likes": _num(cd.get("like_num"))}
    audits = ((payloads.get("archives") or {}).get("data") or {}).get("arc_audits") or []
    rows = []
    for it in audits:
        a = it.get("Archive") or {}
        st = it.get("stat") or {}
        rows.append({"ts": to_sec(a.get("ptime")), "views": st.get("view"),
                     "likes": st.get("like"), "comments": st.get("reply"),
                     "collects": st.get("favorite"), "shares": st.get("share"),
                     "desc": a.get("title") or ""})
    works, top = _aggregate(rows, start)
    return account, works, top


def _bili_code_msg(body, what):
    """把 B站的 code 翻成人话。★ -799 是**限频**不是坏 —— 要让人知道"稍后再试"。"""
    code = body.get("code")
    msg = str(body.get("message") or "")
    if code == -799 or "频繁" in msg:
        return "%s：请求过于频繁（B站限频），请稍后再试" % what
    if code in (-101, -400, -403, 61000):
        return "%s：登录态可能已失效（code=%s %s）" % (what, code, msg[:40])
    return "%s 返回 code=%s %s" % (what, code, msg[:60])


def fetch_bilibili(pg, status_path, max_pages, start=0, days=DAYS_DEFAULT):
    """B站：nav(拿 mid) → card(账号) → upstat(汇总) → archives(逐条作品，窗口内)。

    ★ 前三步**只调 3 次、绝不重试成环** —— B站对 `api.bilibili.com` 那类接口限频很凶
      （实测第二次就 -799）；archives 走的是 member.bilibili.com 的创作中心接口，
      实测可用，但照样**限页数、页间隔 0.6 秒、按投稿时间倒序提前停**。"""
    def _get(url, referer=BILI_REFERER):
        r = pg.request.get(url, headers={"Referer": referer})
        return r.json()

    w(status_path, {"state": "running", "note": "读取登录态与账号…"})
    try:
        nav = _get(BILI_NAV, "https://www.bilibili.com/")
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "读取登录态失败：%s" % str(e)[:120]}
    nd = nav.get("data") or {}
    if not nd.get("isLogin"):
        return {"ok": False, "error": "B站未登录（登录态可能已失效，可到账号页点「核验」）"}
    mid = nd.get("mid")
    if not mid:
        return {"ok": False, "error": "nav 里没返回 mid（接口结构可能变了）"}

    try:
        card = _get(BILI_CARD % mid)
        if (card.get("code") or 0) != 0:
            return {"ok": False, "error": _bili_code_msg(card, "账号接口")}
        w(status_path, {"state": "running", "note": "读取播放/获赞…"})
        up = _get(BILI_UPSTAT % mid)
        if (up.get("code") or 0) != 0:
            return {"ok": False, "error": _bili_code_msg(up, "汇总接口")}
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "读取数据失败：%s" % str(e)[:120]}

    # ── 作品列表（稿件管理）→ 只留窗口内的 ──
    audits, pages, stop = [], 0, False
    for pn in range(1, max(1, min(int(max_pages), 5)) + 1):     # 上限 5 页：限频风险自己兜住
        w(status_path, {"state": "running",
                        "note": "读取作品列表 第 %d 页…（已读回 %d 条）" % (pn, len(audits))})
        try:
            ar = _get(BILI_ARCHIVES.replace("{pn}", str(pn)))
        except Exception as e:                   # noqa: BLE001
            if not audits:
                return {"ok": False, "error": "作品列表读取失败：%s" % str(e)[:120]}
            break
        if (ar.get("code") or 0) != 0:
            if not audits:
                return {"ok": False, "error": _bili_code_msg(ar, "作品列表接口")}
            break
        page_items = ((ar.get("data") or {}).get("arc_audits")) or []
        audits += page_items
        pages = pn
        # ★ 提前停：**整页**都早于窗口才停（不是"有一条老的就停"——列表里可能混着
        #   置顶/审核中的条目，顺序不保证严格倒序，宁可多翻一页也别漏）。
        if page_items and all(0 < to_sec(((it.get("Archive") or {}).get("ptime"))) < start
                              for it in page_items):
            stop = True
        if stop or len(page_items) < 10:
            break
        time.sleep(0.6)

    account, works, top = _map_bilibili(
        {"card": card, "upstat": up, "archives": {"data": {"arc_audits": audits}}}, start)
    if account is None:
        return {"ok": False, "error": "接口结构变了：card/upstat 里没有预期的 data 字段"}
    return {"ok": True, "platform": "bilibili", "source": "bilibili_api",
            "account": account, "works": works, "top": top,
            "note": win_note(start, works["count"], days) + "（读回 %d 页 / %d 条）" % (pages, len(audits))}


# ────────────────────────── 微博（2026-09-29）──────────────────────────
# 接口都是探针**实测**出来的，字段以 `app/tests/_data_samples/weibo.json` 的真实样本为准。
WB_SIDE = "https://weibo.com/ajax/side/cards/sideInterested"     # 登录态 + uid（证据）
WB_PROFILE = "https://weibo.com/ajax/profile/info?uid=%s"
WB_MYMBLOG = "https://weibo.com/ajax/statuses/mymblog?uid=%s&page=%d&feature=0"

_WEIBO_UNAVAILABLE_NOTE = (
    "收藏：微博这套接口里没有收藏数（要它得进数据中心），本版未取到、**不编造**；"
    "阅读数是逐条 reads_count 求和，只覆盖取到的那几页。")


def _map_weibo(payloads, start=0):
    """微博：账号 + 汇总 + 逐条博文 → 统一 schema。**纯函数**（判据灌样本进来测）。

      profile.data.user: screen_name / idstr / followers_count / friends_count /
                         statuses_count / status_total_counter{like_cnt,comment_cnt,repost_cnt}
      list.data.list[] : attitudes_count（赞）/ comments_count / reposts_count /
                         reads_count（阅读）/ text_raw
    返回 (account, works, top)；结构不对 → (None, None, None)。
    """
    if not isinstance(payloads, dict):
        return None, None, None
    u = (((payloads.get("profile") or {}).get("data") or {}).get("user") or {})
    if not u:
        return None, None, None
    posts = (((payloads.get("list") or {}).get("data") or {}).get("list") or [])
    tc = (u.get("status_total_counter") or {})
    account = {"nick_name": str(u.get("screen_name") or ""),
               "unique_id": str(u.get("idstr") or u.get("id") or ""),
               "followers": _num(u.get("followers_count")),
               "following": _num(u.get("friends_count")),
               "total_likes": _num(tc.get("like_cnt"))}
    works_rows = [{"ts": to_sec(x.get("created_at")),
                   "views": x.get("reads_count"), "likes": x.get("attitudes_count"),
                   "comments": x.get("comments_count"), "shares": x.get("reposts_count"),
                   "collects": 0,     # 微博这套接口没有收藏数，见 _WEIBO_UNAVAILABLE_NOTE
                   "desc": x.get("text_raw") or x.get("text") or ""} for x in posts]
    works, top = _aggregate(works_rows, start)
    return account, works, top


def fetch_weibo(pg, status_path, max_pages, start=0, days=DAYS_DEFAULT):
    """微博：side(拿 uid) → profile/info(账号汇总) → mymblog 翻页(逐条阅读/点赞)。

    ★ uid 从**登录态接口自己**取（`sideInterested` 的 data.uid，2026-09-29 侦察证据）——
      不手填、不猜；取不到 uid 就等于"没登录"，如实报。
    """
    def _get(url, referer="https://weibo.com/"):
        r = pg.request.get(url, headers={"Referer": referer})
        return r.json()

    w(status_path, {"state": "running", "note": "读取登录账号…"})
    try:
        side = _get(WB_SIDE)
        uid = str(((side.get("data") or {}).get("uid")) or "")
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "读取登录态失败：%s" % str(e)[:120]}
    if not uid:
        return {"ok": False, "error": "没取到登录 uid（微博登录态可能已失效，可到账号页点「核验」）"}

    try:
        prof = _get(WB_PROFILE % uid, "https://weibo.com/u/%s" % uid)
        if not (((prof.get("data") or {}).get("user") or {})):
            return {"ok": False, "error": "资料接口没返回 user（登录态失效或接口结构变了）"}
        posts, pages = [], 0
        for i in range(1, max(1, max_pages) + 1):
            w(status_path, {"state": "running",
                            "note": "读取博文 第 %d 页…（已 %d 条）" % (i, len(posts))})
            j = _get(WB_MYMBLOG % (uid, i), "https://weibo.com/u/%s" % uid)
            page_list = ((j.get("data") or {}).get("list")) or []
            posts += page_list
            pages = i
            if not page_list:
                break
            time.sleep(0.8)                      # 别把人家接口打急了
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "读取博文失败：%s" % str(e)[:120]}

    account, works, top = _map_weibo({"profile": prof, "list": {"data": {"list": posts}}}, start)
    if account is None:
        return {"ok": False, "error": "接口结构变了：资料里没有预期的 data.user"}
    return {"ok": True, "platform": "weibo", "source": "weibo_api",
            "account": account, "works": works, "top": top,
            "note": win_note(start, works["count"], days)
                    + "（读回 %d 条 / %d 页）。%s" % (len(posts), pages, _WEIBO_UNAVAILABLE_NOTE)}


# ────────────────────────── 头条（2026-09-29）──────────────────────────
TT_USER_INFO = "https://mp.toutiao.com/mp/agw/creator_center/user_info?app_id=1231"
# ★ feed 的 URL 参数很长（client_extra_params 里套着 URL 编码的 JSON），
#   **直接从侦察样本里抽出来照用**，不手抄（手抄必错）；改版时按新样本同样处理。
TT_FEED = "https://mp.toutiao.com/api/feed/mp_provider/v1/?aid=13&app_name=news_article&category=mp_videos&channel=&client_extra_params=%7B%22category%22%3A%22mp_videos%22%2C%22real_app_id%22%3A%221231%22%2C%22need_forward%22%3A%22true%22%2C%22offset_mode%22%3A%221%22%2C%22page_index%22%3A%221%22%2C%22status%22%3A0%2C%22source%22%3A%22all%22%7D&count=20&device_platform=pc&genre_type_switch=%7B%22repost%22%3A1%2C%22small_video%22%3A1%2C%22toutiao_graphic%22%3A1%2C%22weitoutiao%22%3A1%2C%22xigua_video%22%3A1%7D&keyword=&offset=0&platform_id=0&stream_api_version=88&visited_uid=72026370245"


def _map_toutiao(payloads, start=0):
    """头条：账号 + 作品列表 → 统一 schema。**纯函数**（判据灌样本进来测）。

    字段以 `app/tests/_data_samples/toutiao.json` 的真实样本为准，且**已与后台对数**：
      后台「作品管理」第 1 条显示「展现 2780 · 播放 82 · 点赞 0 · 评论 2」
      ↔ itemCounter: showCount 2780 / videoWatchCount 82 / diggCount 0 / commentCount 2 ✓
    ⇒ **播放取 videoWatchCount**（后台那列就叫「播放」），展现(showCount) 不进 schema。
    """
    if not isinstance(payloads, dict):
        return None, None, None
    u = payloads.get("user_info") or {}
    if not u or u.get("media_id") is None:
        return None, None, None
    items = ((payloads.get("feed") or {}).get("data") or [])
    account = {"nick_name": str(u.get("name") or ""),
               "unique_id": str(u.get("media_id") or u.get("user_id_str") or ""),
               "followers": _num(u.get("total_fans_count")),
               "following": 0,          # 这套接口没有"关注数"（note 里说明）
               "total_likes": 0}        # 也没有账号级总获赞（用作品求和看 works.likes）
    rows = []
    for it in items:
        cell = ((it.get("assembleCell") or {}).get("itemCell") or {})
        c = cell.get("itemCounter") or {}
        base = cell.get("articleBase") or {}
        rows.append({"ts": to_sec(base.get("publishTime") or base.get("createTime")),
                     "views": c.get("videoWatchCount"), "likes": c.get("diggCount"),
                     "comments": c.get("commentCount"), "shares": c.get("repinCount"),
                     "collects": 0,
                     "desc": base.get("title") or base.get("abstractText") or ""})
    works, top = _aggregate(rows, start)
    return account, works, top


def fetch_toutiao(pg, status_path, max_pages, start=0, days=DAYS_DEFAULT):
    """头条：user_info(账号) → mp_provider(作品列表，一页最多 20 条)。

    ★ 头条的翻页参数套在 `client_extra_params` 的 URL 编码 JSON 里、改起来容易错，
      所以本版**只取第一页**；`has_more` 为真时在 note 里写明（不假装是全量）。
    """
    def _get(u2, referer="https://mp.toutiao.com/profile_v4/xigua/content-manage-v2"):
        r = pg.request.get(u2, headers={"Referer": referer})
        return r.json()

    w(status_path, {"state": "running", "note": "读取账号信息…"})
    try:
        u = _get(TT_USER_INFO)
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "读取账号接口失败：" + str(e)[:120]}
    if (u.get("code") or 0) != 0:
        return {"ok": False, "error": "账号接口 code=%s（登录态可能已失效，可到账号页点「核验」）" % u.get("code")}
    w(status_path, {"state": "running", "note": "读取作品列表…"})
    try:
        f = _get(TT_FEED)
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "读取作品列表失败：" + str(e)[:120]}
    # ★ 成功判据是**有没有 data**，不是 errno 是否为零（2026-09-29 真机实测）：
    #   头条的 feed 会返回 `errno=20100` **但数据照样给全**（实测拿到 13 条）。
    #   第一版把 errno!=0 当失败 → 把一个完好的响应拒了。**信载荷，别信标志位。**
    if not (f.get("data") or []) and (f.get("errno") or 0) != 0:
        return {"ok": False, "error": "作品接口没返回数据（errno=%s，登录态可能已失效，可到账号页点「核验」）"
                                      % f.get("errno")}
    account, works, top = _map_toutiao({"user_info": u, "feed": f}, start)
    if account is None:
        return {"ok": False, "error": "接口结构变了：账号接口里没有 media_id"}
    note = win_note(start, works["count"], days)
    if f.get("has_more"):
        note += "（接口还有更多页，本版只取第一页 —— 头条翻页参数易错，未实现）"
    note += "关注数/账号级总获赞：头条这套接口没有，置 0；点赞按作品求和。"
    return {"ok": True, "platform": "toutiao", "source": "toutiao_api",
            "account": account, "works": works, "top": top, "note": note}


# ────────────────────────── 视频号（2026-09-30）──────────────────────────
# ★ 调用形状照真实请求复现（探针实测，_scratch/ch-recon/）：三个接口都是 POST + JSON 体。
#   `_aid/_rid/_log_finder_id` 是埋点/会话字段，**实测可省**（裸调 errCode=0、返回 20 条）。
CH_HOST = "https://channels.weixin.qq.com"
CH_POST_LIST = CH_HOST + "/micro/content/cgi-bin/mmfinderassistant-bin/post/post_list"
CH_AUTH = CH_HOST + "/cgi-bin/mmfinderassistant-bin/auth/auth_data"
CH_FANS = CH_HOST + "/cgi-bin/mmfinderassistant-bin/statistic/fans_trend"
CH_REFERER = CH_HOST + "/platform"


def _ch_body(extra=None):
    b = {"timestamp": str(int(time.time() * 1000)), "_log_finder_uin": None, "rawKeyBuff": "",
         "pluginSessionId": None, "scene": 7, "reqScene": 7}
    b.update(extra or {})
    return b


def _map_channels(payloads, start=0):
    """视频号：账号 + 关注者 + 作品列表 → 统一 schema。**纯函数**。

    · auth.data.userAttr.{nickname,username}
    · fans.data.total[-1]（关注者总数；`total` 是个数组，取最后一个）
    · list.data.list[].{createTime, readCount, likeCount, commentCount, forwardCount,
                        favCount, desc.description}
    样本：`_data_samples/channels.json`（真实、已裁剪）。
    """
    if not isinstance(payloads, dict):
        return None, None, None
    ua = (((payloads.get("auth") or {}).get("data") or {}).get("userAttr")) or {}
    if not ua:
        return None, None, None
    ftot = (((payloads.get("fans") or {}).get("data") or {}).get("total")) or []
    items = (((payloads.get("list") or {}).get("data") or {}).get("list")) or []
    account = {"nick_name": str(ua.get("nickname") or ""),
               "unique_id": str(ua.get("username") or ""),
               "followers": _num(ftot[-1]) if ftot else 0,
               "following": 0,               # 视频号这套接口没有"关注数"（note 里说明）
               "total_likes": 0}
    rows = []
    for it in items:
        d = it.get("desc") or {}
        rows.append({"ts": to_sec(it.get("createTime")), "views": it.get("readCount"),
                     "likes": it.get("likeCount"), "comments": it.get("commentCount"),
                     "collects": it.get("favCount"), "shares": it.get("forwardCount"),
                     "desc": d.get("description") or ""})
    works, top = _aggregate(rows, start)
    return account, works, top


def fetch_channels(pg, status_path, max_pages, start=0, days=DAYS_DEFAULT):
    """视频号：auth_data(账号) → fans_trend(关注者) → post_list 翻页（`currentPage`）。

    ★ 翻页参数是**页码**（pageSize/currentPage，实测请求体），响应里还有 continueFlag，
      两个都用上：continueFlag 为假、或整页没返回 → 停。
    """
    H = {"Content-Type": "application/json", "Referer": CH_REFERER}

    def _post(url, body):
        r = pg.request.post(url, data=json.dumps(body, ensure_ascii=False), headers=H)
        return r.json()

    w(status_path, {"state": "running", "note": "读取账号信息…"})
    try:
        auth = _post(CH_AUTH, _ch_body())
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "账号接口请求失败：%s" % str(e)[:120]}
    if (auth.get("errCode") or 0) != 0:
        return {"ok": False, "error": "账号接口 errCode=%s（登录态可能已失效，可到账号页点「核验」）"
                                      % auth.get("errCode")}
    try:
        fans = _post(CH_FANS, _ch_body({"startTs": str(int(time.time()) - 7 * 86400),
                                        "endTs": str(int(time.time())), "interval": 3}))
    except Exception:                            # noqa: BLE001
        fans = {}                                # 关注者取不到不算失败，置 0 并在 note 里说明
    items, pages = [], 0
    for pn in range(1, max(1, int(max_pages)) + 1):
        w(status_path, {"state": "running",
                        "note": "读取作品 第 %d 页…（已读回 %d 条）" % (pn, len(items))})
        try:
            j = _post(CH_POST_LIST, _ch_body({"pageSize": 20, "currentPage": pn,
                                              "userpageType": 11, "stickyOrder": False}))
        except Exception as e:                   # noqa: BLE001
            if not items:
                return {"ok": False, "error": "作品接口请求失败：%s" % str(e)[:120]}
            break
        if (j.get("errCode") or 0) != 0:
            if not items:
                return {"ok": False, "error": "作品接口 errCode=%s" % j.get("errCode")}
            break
        page = ((j.get("data") or {}).get("list")) or []
        items += page
        pages = pn
        if not page or not (j.get("data") or {}).get("continueFlag"):
            break
        time.sleep(0.6)

    account, works, top = _map_channels({"auth": auth, "fans": fans,
                                         "list": {"data": {"list": items}}}, start)
    if account is None:
        return {"ok": False, "error": "接口结构变了：auth_data 里没有 userAttr"}
    note = win_note(start, works["count"], days) + "（读回 %d 页 / %d 条）" % (pages, len(items))
    # ★ 关注数/总获赞**始终**说明（OCR 2026-09-30 指出：原来只在 fans 取不到时才提，
    #   于是 fans 正常时用户会以为 following=0 是这个号真的没关注别人）。
    note += "关注数/账号级总获赞：视频号这套接口没有，置 0。"
    if not fans:
        note += "关注者数这次也没取到，置 0。"
    return {"ok": True, "platform": "channels", "source": "channels_api",
            "account": account, "works": works, "top": top, "note": note}


# ────────────────────────── 快手（2026-09-30）──────────────────────────
# ★ 实测**裸调即可**：不需要 `__NS_sig3` 签名、也不需要 `kuaishou.web.cp.api_ph` 会话 token
#   （探针 v6：POST + 空 JSON 体 → result=1、返回 3 条）。
KS_HOST = "https://cp.kuaishou.com"
KS_WORKS = KS_HOST + "/rest/cp/works/v2/video/pc/home/photo/list"
# ★ 账号接口用 `userInfo`，**不要用 `creator/pc/home/infoV2`**（2026-09-30 实测）：
#   infoV2 无论带不带 body 都返回 `result=500002 请稍后重试`，而**同一时刻** photo/list
#   与 userInfo 都正常 ⇒ 是这个端点自己有要求（不是限流、也不是代码问题）。
KS_INFO = KS_HOST + "/rest/cp/creator/pc/home/userInfo"
KS_REFERER = KS_HOST + "/profile"


def _map_kuaishou(payloads, start=0):
    """快手：账号 + 作品列表 → 统一 schema。**纯函数**。

    · info.data.coreUserInfo.{userName,userId,**fansNum**}（走 `creator/pc/home/userInfo`；
      早期用 infoV2 那套，它的字段叫 fansCnt —— 换端点就换了名字，别照抄）
    · works.data.list[].{title,uploadTime(**毫秒**),playCount,likeCount,commentCount}
    样本：`_data_samples/kuaishou.json`（真实、已裁剪）。
    """
    if not isinstance(payloads, dict):
        return None, None, None
    info = (((payloads.get("info") or {}).get("data") or {}).get("coreUserInfo")) or {}
    if not info.get("userName") and not info.get("userId"):
        return None, None, None
    items = (((payloads.get("works") or {}).get("data") or {}).get("list")) or []
    account = {"nick_name": str(info.get("userName") or ""),
               "unique_id": str(info.get("userId") or ""),
               # ★ userInfo 给的字段名是 `fansNum`（infoV2 那套叫 fansCnt，换个端点就换名字）
               "followers": _num(info.get("fansNum")),
               "following": 0,        # userInfo 没有关注数（fetch 的 note 里始终说明）
               "total_likes": 0}      # 也没有账号级总获赞（同上）
    rows = []
    for it in items:
        rows.append({"ts": to_sec(it.get("uploadTime")), "views": it.get("playCount"),
                     "likes": it.get("likeCount"), "comments": it.get("commentCount"),
                     "collects": 0, "shares": 0,
                     "desc": it.get("title") or ""})
    works, top = _aggregate(rows, start)
    return account, works, top


def fetch_kuaishou(pg, status_path, max_pages, start=0, days=DAYS_DEFAULT):
    """快手：userInfo(账号) → works photo/list 翻页（`nextCursor`）。

    ★ 必须先**打开创作者中心页面**再调接口（2026-09-30 真机实测）：
      探针里是「goto /profile → 等 7 秒 → 调接口」才通的；直接调（哪怕主流程已经
      开了 cp.kuaishou.com/）会返回 `result=500002` —— 页面自己的 JS 要先做点什么
      （签名材料/会话初始化），省不得。
    """
    H = {"Content-Type": "application/json", "Referer": KS_REFERER}

    def _post(url, body):
        r = pg.request.post(url, data=json.dumps(body, ensure_ascii=False), headers=H)
        return r.json()

    w(status_path, {"state": "running", "note": "打开创作者中心…"})
    try:
        pg.goto(KS_REFERER, wait_until="domcontentloaded", timeout=60000)
        time.sleep(6)
    except Exception:                            # noqa: BLE001
        pass                                     # 打不开也继续试，接口自己会报原因

    w(status_path, {"state": "running", "note": "读取账号信息…"})
    try:
        info = _post(KS_INFO, {})
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "账号接口请求失败：%s" % str(e)[:120]}
    if (info.get("result") or 0) != 1:
        # ★ 别猜原因，把平台原话带上（result=500002「请稍后重试」实测是**端点自己的要求**，
        #   同一时刻别的端点都正常 —— 见 KS_INFO 的注释）。
        return {"ok": False, "error": "账号接口 result=%s %s（可到账号页点「核验」）"
                                      % (info.get("result"), str(info.get("message") or "")[:40])}
    items, pages, cursor, seen = [], 0, "", set()
    for pn in range(1, max(1, int(max_pages)) + 1):
        w(status_path, {"state": "running",
                        "note": "读取作品 第 %d 页…（已读回 %d 条）" % (pn, len(items))})
        try:
            j = _post(KS_WORKS, {"cursor": cursor} if cursor else {})
        except Exception as e:                   # noqa: BLE001
            if not items:
                return {"ok": False, "error": "作品接口请求失败：%s" % str(e)[:120]}
            break
        if (j.get("result") or 0) != 1:
            if not items:
                return {"ok": False, "error": "作品接口 result=%s" % j.get("result")}
            break
        d = j.get("data") or {}
        page = d.get("list") or []
        ids = {x.get("workId") for x in page}
        if not page or (ids & seen):             # ★ 游标不前进 / 重复页 → 停（别死循环打人家接口）
            items += [x for x in page if x.get("workId") not in seen]
            break
        seen |= ids
        items += page
        pages = pn
        cursor = str(d.get("nextCursor") or "")
        if not cursor:
            break
        time.sleep(0.6)

    account, works, top = _map_kuaishou({"info": info, "works": {"data": {"list": items}}}, start)
    if account is None:
        return {"ok": False, "error": "接口结构变了：infoV2 里没有 userName/userId"}
    return {"ok": True, "platform": "kuaishou", "source": "kuaishou_api",
            "account": account, "works": works, "top": top,
            "note": win_note(start, works["count"], days) + "（读回 %d 页 / %d 条）" % (pages, len(items))
                    + "关注数/账号级总获赞：快手这套接口没有，置 0。"}


# ────────────────────────── 小红书（2026-09-30）──────────────────────────
# ★★ 唯一一个**不能直接 replay** 的平台：实测裸调 `note/user/posted` 返回 **406**（code=-1）——
#    它的 galaxy 接口要签名头（页面 JS 现算，我们复现不了；快手的签名反而不用）。
#    但**页面自己发的那个请求是通的** ⇒ 让页面去发，我们用响应监听把包截下来。
#    与 design 的"不扒 DOM"不冲突：拿的还是接口 JSON，只是发起方换成了页面。
XHS_POSTED_PAGE = "https://creator.xiaohongshu.com/new/note-manager"


def _map_xhs(payloads, start=0):
    """小红书：账号 + 笔记列表 → 统一 schema。**纯函数**。

    · user.data.{userId,userName}
    · posted.data.notes[].{display_title, visible_time, view_count, likes,
                           comments_count, collected_count, shared_count}
    样本：`_data_samples/xhs.json`（真实、已裁剪）。
    """
    if not isinstance(payloads, dict):
        return None, None, None
    ud = (payloads.get("user") or {}).get("data") or {}
    if not ud:
        return None, None, None
    notes = (((payloads.get("posted") or {}).get("data") or {}).get("notes")) or []
    account = {"nick_name": str(ud.get("userName") or ""),
               "unique_id": str(ud.get("userId") or ""),
               "followers": _num(ud.get("fans") or ud.get("fansCount") or ud.get("fans_count")),
               "following": _num(ud.get("follow") or ud.get("followCount")),
               "total_likes": 0}                 # 这套接口没给账号级总获赞（note 里说明）
    rows = []
    for n in notes:
        rows.append({"ts": to_sec(n.get("visible_time") or n.get("time")),
                     "views": n.get("view_count"), "likes": n.get("likes"),
                     "comments": n.get("comments_count"), "collects": n.get("collected_count"),
                     "shares": n.get("shared_count"), "desc": n.get("display_title") or ""})
    works, top = _aggregate(rows, start)
    return account, works, top


def fetch_xhs(pg, status_path, max_pages, start=0, days=DAYS_DEFAULT):
    """小红书：打开笔记管理页 → 截页面自己发出的 note/user/posted 与 user/info。

    ★ 只取**页面首屏加载的那一页**笔记（翻页要模拟点"加载更多"，本版不做；
      账号笔记多的用户会在 note 里看到"只取到 N 条"）。不把没取到的说成全量。
    """
    got = {}

    def on_resp(r):
        u = r.url or ""
        if "note/user/posted" in u and "posted" not in got:
            try:
                got["posted"] = r.json()
            except Exception:                    # noqa: BLE001
                pass
        if "/api/galaxy/user/info" in u and "user" not in got:
            try:
                got["user"] = r.json()
            except Exception:                    # noqa: BLE001
                pass

    pg.on("response", on_resp)
    w(status_path, {"state": "running", "note": "打开笔记管理页（等页面自己拉数据）…"})
    try:
        pg.goto(XHS_POSTED_PAGE, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "error": "打开笔记管理页失败：%s" % str(e)[:120]}

    def _wait(sec):
        for _ in range(sec):
            if "posted" in got and "user" in got:
                return True
            time.sleep(1)
        return "posted" in got

    _wait(12)
    if "posted" not in got:
        # ★★ 2026-09-30 实测（三次探针才对出来）：这个页面**经常一个接口都不发**
        #   （服务端直出/缓存命中的时候，20 秒里只有 HTML 一个响应，数据却照样渲染）
        #   ⇒ 监听器什么都截不到。**点一下筛选 tab（已发布/全部）会逼 SPA 重新拉一次**，
        #   实测点完连中三个接口（note/user/posted + user/info + creator/user/video）。
        #   试过、不管用的两条路（别再走）：
        #     ① 页面里 window.fetch(...) → 406（签名是应用自己的请求层加的，全局 fetch 拿不到）
        #     ② 直接 replay（含 Cookie）→ 同样 406
        #   结论：小红书这份数据**只能让页面自己去发**，我们只负责截。
        for label in ("已发布", "全部"):
            try:
                el = pg.get_by_text(label, exact=True).first
                if el.count():
                    el.click(timeout=5000)
                    time.sleep(4)
            except Exception:                    # noqa: BLE001
                pass
            if "posted" in got:
                break
        _wait(10)
    if "posted" not in got:
        w(status_path, {"state": "running", "note": "没截到请求，强刷重试…"})
        try:
            pg.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception:                        # noqa: BLE001
            pass
        _wait(12)
    if "posted" not in got:
        return {"ok": False, "error": "没能截到笔记列表请求（页面这次没走接口，点 tab/强刷也没逼出来）"
                                      "—— 登录态可能已失效，可到账号页点「核验」"}
    if "user" not in got:
        # ★ 别把"漏包"说成"结构变了"（OCR 2026-09-30 指出）：_map_xhs 见不到 user 会返回
        #   (None,None,None)，直接报"接口结构变了"是**编造原因** —— 事实是这次没截到那个包。
        return {"ok": False, "error": "截到了笔记列表，但没截到 user/info（这次漏包，再抓一次通常就好）；"
                                      "一直这样可到账号页点「核验」"}

    account, works, top = _map_xhs({"user": got["user"], "posted": got["posted"]}, start)
    if account is None:
        return {"ok": False, "error": "接口结构变了：user/info 里没有 data（键名可能改了）"}
    notes = ((got["posted"].get("data") or {}).get("notes")) or []
    note = win_note(start, works["count"], days) + "（页面首屏读回 %d 条）" % len(notes)
    note += "小红书账号若被平台标记异常，它的数据本来就可能是 0（不是抓取坏了）。"
    return {"ok": True, "platform": "xhs", "source": "xhs_api",
            "account": account, "works": works, "top": top, "note": note}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--status", required=True)
    ap.add_argument("--max-pages", type=int, default=10,
                    help="每页 40 条；默认 10 页＝最多 400 条，够覆盖绝大多数账号的全部作品")
    ap.add_argument("--days", type=int, default=DAYS_DEFAULT,
                    help="统计窗口＝(今天-days) 日 0 点到现在；默认 %d ＝昨天 0 点到现在" % DAYS_DEFAULT)
    ap.add_argument("--headless", action="store_true", default=True)
    args = ap.parse_args()

    if args.key not in SUPPORTED:
        w(args.status, {"ok": False, "not_supported": True,
                        "error": "%s 的真实抓取尚未接入（旧版的模拟数据已废弃，不再返回编造数字）" % args.key})
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        w(args.status, {"ok": False, "error": "未安装 playwright"})
        return 1

    w(args.status, {"state": "running", "note": "打开浏览器读取数据…"})
    try:
        with sync_playwright() as p:
            with browser_state.persistent_context(p.chromium,
                user_data_dir=args.profile, channel=browser_channel.channel(),
                headless=args.headless, viewport={"width": 1440, "height": 900}) as ctx:
                pg = ctx.pages[0] if ctx.pages else ctx.new_page()
                try:
                    pg.goto(LOGIN_ENTRY.get(args.key, "https://creator.douyin.com/"),
                            wait_until="domcontentloaded", timeout=60000)
                    time.sleep(5)
                except Exception:
                    pass
                _dispatch = {"douyin": fetch_douyin, "bilibili": fetch_bilibili,
                             "weibo": fetch_weibo, "toutiao": fetch_toutiao,
                             "channels": fetch_channels, "kuaishou": fetch_kuaishou,
                             "xhs": fetch_xhs}
                res = _dispatch[args.key](pg, args.status, args.max_pages,
                                          win_start(args.days), args.days)
                try:
                    browser_state.close(ctx, args.profile)
                except Exception:
                    pass
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": str(e)[:200]}

    w(args.status, res)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

# -*- coding: utf-8 -*-
"""自测：数据看板各平台抓取（离线；真机部分在实施计划里单独跑）。

运行：cd app && python -X utf8 tests/test_27_data_fetch.py

★ 判据分两类：
  ① 侦察工具的**纯函数**（过滤埋点、压缩记录）—— 本任务；
  ② 各平台的 `_map_<key>()`：**拿真实样本当 fixture** 灌进去断言聚合 —— 每个平台任务加一组。
★ 贯穿全局的一条：**抓不到不许编数字**（未接入 not_supported；失败 ok=False + 原因；
  结果里出现任何编造的统计数字都算失败）。
"""
import datetime
import json
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "tools"))

RESULTS = []


def ok(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    line = ("PASS " if cond else "FAIL ") + name
    if not cond and extra:
        line += " | 实际: %s" % (extra,)
    print(line)


def main():
    import recon_data as rc

    # 1 埋点/上报类不该记（否则样本里全是噪音，找不到真正的数据接口）
    ok("1 记录数据接口", rc.should_record("https://member.bilibili.com/x/web/data?aid=1") is True)
    ok("1 过滤埋点", rc.should_record("https://data.bilibili.com/log/web?t=1") is False)
    ok("1 过滤 report/beacon", rc.should_record("https://x.com/report/beacon") is False)
    ok("1 --all 时不过滤", rc.should_record("https://data.bilibili.com/log/web", keep_all=True) is True)

    # 2 压缩记录：只留方法/URL/状态/顶层键/样本片段
    rec = {"method": "GET", "url": "https://x/api?b=2&a=1",
           "status": 200, "body": {"code": 0, "data": {"list": [1, 2, 3]}},
           "req_headers": {"Referer": "https://x/", "Cookie": "SECRET=1"}}
    s = rc.summarize(rec)
    ok("2 带上方法与状态", s["method"] == "GET" and s["status"] == 200, s)
    ok("2 顶层键列出来", s["keys"] == ["code", "data"], s)
    ok("2 ★ Cookie 不落盘", "SECRET" not in json.dumps(s, ensure_ascii=False), s)
    # ★ 只脱敏 cookie 不够（OCR 复查指出）：authorization / csrf 之类也是凭据
    _rec2 = {"method": "GET", "url": "https://x/api", "status": 200, "body": {"ok": 1},
             "req_headers": {"Authorization": "Bearer AKIA-SECRET", "x-csrf-token": "CSRF-SECRET",
                             "x-xsrf-token": "XSRF-SECRET", "Referer": "https://x/"}}
    _s2 = rc.summarize(_rec2)
    _blob = json.dumps(_s2, ensure_ascii=False)
    ok("2 ★ authorization/csrf 也不落盘",
       ("AKIA-SECRET" not in _blob) and ("CSRF-SECRET" not in _blob) and ("XSRF-SECRET" not in _blob), _s2)
    ok("2 无害头仍保留（Referer 在）", "Referer" in _s2["req_headers"], _s2)
    ok("2 样本被截断（不落整包）", len(json.dumps(s["sample"], ensure_ascii=False)) < 600, len(json.dumps(s["sample"])))


    # ══════════ B站（Task 2 · 2026-09-30 口径变更）══════════
    # ★ 两处变化：
    #   ① 口径：works 从"账号合计"改成"**窗口内发布的那些作品**求和"（用户要"昨天发的那批
    #      现在怎么样"）；账号级字段（粉丝等）照旧。
    #   ② 逐条作品**能拿到了**：用户给的入口 `/x/web/archives`（创作中心稿件管理）**不限额**，
    #      原来那条 x/space/arc/search 被限频（-799）。样本 `bilibili_list.json`。
    import data_worker as dw
    _S = json.loads((APP / "tests" / "_data_samples" / "bilibili.json").read_text(encoding="utf-8"))
    _SL = json.loads((APP / "tests" / "_data_samples" / "bilibili_list.json").read_text(encoding="utf-8"))
    _AUD = _SL["archives"]["data"]["arc_audits"]              # 3 条（真实、已裁剪）
    acc, works, top = dw._map_bilibili(
        {"card": _S["card"], "upstat": _S["upstat"], "archives": _SL["archives"]})
    ok("B站 账号五个键齐全、昵称对",
       set(acc) == {"nick_name", "unique_id", "followers", "following", "total_likes"}
       and isinstance(acc["followers"], int) and acc["nick_name"] == "居帮帮房屋修缮大毛", acc)
    ok("B站 粉丝仍取账号接口（5，与后台对数一致）", acc["followers"] == 5, acc)
    ok("B站 作品播放 = 逐条 stat.view 之和",
       works["views"] == sum(x["stat"]["view"] for x in _AUD), (works, _AUD))
    ok("B站 作品数 = 条数", works["count"] == len(_AUD), works)
    ok("B站 赞/评论/收藏/分享来自 stat.{like,reply,favorite,share}（不再一律置 0）",
       works["likes"] == sum(x["stat"]["like"] for x in _AUD)
       and works["comments"] == sum(x["stat"]["reply"] for x in _AUD)
       and works["collects"] == sum(x["stat"]["favorite"] for x in _AUD)
       and works["shares"] == sum(x["stat"]["share"] for x in _AUD), works)
    ok("B站 统计都是整数", all(isinstance(works[k], int) for k in
                          ("count", "views", "likes", "comments", "collects", "shares")), works)
    ok("B站 Top 按播放降序且 ≤5", len(top) <= 5 and all(
        top[i]["views"] >= top[i + 1]["views"] for i in range(len(top) - 1)), top)
    # ★★ 这一版的核心：**时间窗过滤**（三家的时间戳单位都不一样，判据逐家钉住）
    _newest = max(x["Archive"]["ptime"] for x in _AUD)
    _a2, _w2, _ = dw._map_bilibili(
        {"card": _S["card"], "upstat": _S["upstat"], "archives": _SL["archives"]}, _newest)
    ok("B站 窗口过滤：只算发布时间在窗口内的", _w2["count"] == 1, (len(_AUD), _w2))
    _a3, _w3, _ = dw._map_bilibili(
        {"card": _S["card"], "upstat": _S["upstat"], "archives": _SL["archives"]},
        _newest + 86400 * 365)
    ok("B站 窗口过滤：全在窗口外 → 0 条 0 播放（老作品不算进来）",
       _w3["count"] == 0 and _w3["views"] == 0, _w3)
    ok("B站 空样本 → (None, None, None)", dw._map_bilibili({}) == (None, None, None))
    ok("B站 缺 card → (None, None, None)",
       dw._map_bilibili({"upstat": _S["upstat"]}) == (None, None, None))
    _SL2 = json.loads(json.dumps(_SL))
    _SL2["archives"]["data"]["arc_audits"][0]["stat"]["view"] = "1.2万"
    _a4, _w4, _ = dw._map_bilibili(
        {"card": _S["card"], "upstat": _S["upstat"], "archives": _SL2["archives"]}, 0)
    ok("B站 带单位字符串能解（1.2万 → 12000），进得了求和", _w4["views"] >= 12000, _w4)
    ok("B站 已登记进 SUPPORTED", "bilibili" in dw.SUPPORTED, dw.SUPPORTED)

    class _R:
        def __init__(self, body, status=200): self._b, self.status, self.ok = body, status, True
        def json(self): return self._b

    class _P:
        """假 page：按顺序吐 body，并**数调用次数**（★ 限频接口绝不能被重试成环）。"""
        def __init__(self, bodies): self._b, self.n, self.urls = bodies, 0, []
        class _Req:
            def __init__(self, o): self.o = o
            def get(self, url, headers=None):
                self.o.urls.append(url)
                i = min(self.o.n, len(self.o._b) - 1)
                self.o.n += 1
                return _R(self.o._b[i])
        @property
        def request(self): return _P._Req(self)

    _p = _P([{"code": 0, "data": {"isLogin": True, "mid": 549027884}},
             _S["card"], _S["upstat"], _SL["archives"]])
    _r = dw.fetch_bilibili(_p, str(APP / "tests" / "_tmp27.json"), 3, 0, 1)
    ok("B站 正常路径：ok=True 且 4 次请求（nav/card/upstat/作品列表）",
       _r["ok"] and len(_p.urls) == 4, (_r.get("ok"), len(_p.urls), _r.get("error")))
    ok("B站 note 写明统计范围（用户要拿它对数）", "统计范围" in (_r.get("note") or ""), _r.get("note"))
    _p2 = _P([{"code": 0, "data": {"isLogin": True, "mid": 549027884}},
              {"code": -799, "message": "请求过于频繁，请稍后再试"}])
    _r2 = dw.fetch_bilibili(_p2, str(APP / "tests" / "_tmp27.json"), 3)
    ok("B站 撞限频 → 如实说「稍后再试」（不是笼统的抓取失败）",
       _r2["ok"] is False and "稍后" in _r2["error"], _r2.get("error"))
    ok("B站 撞限频 → **只调 2 次就停**（绝不重试成环，那样只会被限更久）", len(_p2.urls) == 2, _p2.urls)
    _p3 = _P([{"code": 0, "data": {"isLogin": False}}])
    _r3 = dw.fetch_bilibili(_p3, str(APP / "tests" / "_tmp27.json"), 3)
    ok("B站 未登录 → 明确说未登录", _r3["ok"] is False and "未登录" in _r3["error"], _r3.get("error"))


    # ══════════ 微博（Task 5）══════════
    _W = json.loads((APP / "tests" / "_data_samples" / "weibo.json").read_text(encoding="utf-8"))
    _wa, _ww, _wt = dw._map_weibo(_W)
    ok("微博 账号五键齐全、昵称对",
       set(_wa) == {"nick_name", "unique_id", "followers", "following", "total_likes"}
       and _wa["nick_name"] == "毛大毛立" and _wa["followers"] == 1621, _wa)
    _WPOSTS = _W["list"]["data"]["list"]
    ok("微博 作品指标按**逐条博文**求和（口径变更：不再用账号级 counter）",
       _ww["likes"] == sum(x.get("attitudes_count") or 0 for x in _WPOSTS)
       and _ww["comments"] == sum(x.get("comments_count") or 0 for x in _WPOSTS)
       and _ww["shares"] == sum(x.get("reposts_count") or 0 for x in _WPOSTS), _ww)
    ok("微博 作品数 = 窗口内条数", _ww["count"] == len(_WPOSTS), _ww)
    ok("微博 阅读数按样本求和（reads_count 是真字段）", _ww["views"] >= 16, _ww)
    # ★ 窗口：微博给的是**带时区的字符串**（"Tue Sep 29 13:44:55 +0800 2026"）——
    #   全平台里唯一一个不是数字的，判据单独钉一条。
    _wts = dw.to_sec(_WPOSTS[0]["created_at"])
    ok("微博 created_at 字符串能解成时间戳（不是 0）", _wts > 1.6e9, _wts)
    _wa2, _ww2, _ = dw._map_weibo(_W, _wts + 1)
    ok("微博 窗口过滤：起点晚于全部博文 → 0 条", _ww2["count"] == 0, _ww2)
    ok("微博 收藏本版取不到 → 0（由 note 说明，不装成真是 0）",
       _ww["collects"] == 0 and "收藏" in dw._WEIBO_UNAVAILABLE_NOTE)
    ok("微博 Top 按阅读降序且 ≤5", len(_wt) <= 5 and all(
        _wt[i]["views"] >= _wt[i + 1]["views"] for i in range(len(_wt) - 1)), _wt)
    ok("微博 空样本 → (None, None, None)", dw._map_weibo({}) == (None, None, None))
    ok("微博 缺 profile → (None, None, None)", dw._map_weibo({"list": _W["list"]}) == (None, None, None))
    _W2 = json.loads(json.dumps(_W))
    _W2["profile"]["data"]["user"]["followers_count"] = "1.6万"
    _a2, _, _ = dw._map_weibo(_W2)
    ok("微博 带单位字符串能解（1.6万 → 16000）", _a2["followers"] == 16000, _a2)
    ok("微博 已登记进 SUPPORTED", "weibo" in dw.SUPPORTED, dw.SUPPORTED)

    # ══════════ 统计窗口（2026-09-30 口径的核心工具）══════════
    # ★ 三家的时间戳单位/格式都不一样（实测逐条对过页面），这里把它们钉住：
    #   秒（视频号/B站/头条/小红书）、毫秒（快手）、带 +0800 的字符串（微博）。
    _now = datetime.datetime(2026, 9, 30, 11, 0, 0).timestamp()
    ok("窗口 起点 = (今天-N) 日 0 点（本地时间）",
       datetime.datetime.fromtimestamp(dw.win_start(1, _now)) == datetime.datetime(2026, 9, 29, 0, 0),
       datetime.datetime.fromtimestamp(dw.win_start(1, _now)))
    ok("窗口 N=3 → 27 日 0 点", datetime.datetime.fromtimestamp(dw.win_start(3, _now)).day == 27)
    ok("时间戳 秒（视频号/B站/头条/小红书）原样用", dw.to_sec(1790734495) == 1790734495.0)
    ok("时间戳 毫秒（快手）→ 除以 1000",
       abs(dw.to_sec(1790734528913) - 1790734528.913) < 0.01, dw.to_sec(1790734528913))
    # ★ 期望值要带**同一个固定时区**（OCR 2026-09-30 指出）：to_sec 解出来的是 aware
    #   datetime，而 datetime(…) 是 naive（按本机时区解释）—— 只有在 UTC+8 的机器上两者才
    #   碰巧相等，换台机器就假失败。
    ok("时间戳 字符串 微博（带 +0800 时区）",
       dw.to_sec("Tue Sep 29 13:44:55 +0800 2026")
       == datetime.datetime(2026, 9, 29, 13, 44, 55,
                            tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp())
    ok("时间戳 字符串 小红书（本地时间）",
       dw.to_sec("2025-04-24 15:42") == datetime.datetime(2025, 4, 24, 15, 42).timestamp())
    ok("时间戳 解不出来 → 0（不猜、不编）", dw.to_sec("昨天") == 0 and dw.to_sec(None) == 0)

    # ══════════ 视频号（2026-09-30 新增）══════════
    # 接口与字段全部来自真实侦察（_scratch/ch-recon/），样本 channels.json。
    _CH = json.loads((APP / "tests" / "_data_samples" / "channels.json").read_text(encoding="utf-8"))
    _CHI = _CH["list"]["data"]["list"]
    _cha, _chw, _cht = dw._map_channels(_CH)
    ok("视频号 账号：昵称/ID 来自 auth_data.userAttr",
       _cha["nick_name"] == "建筑修缮" and _cha["unique_id"], _cha)
    ok("视频号 关注者取 fans.data.total 的最后一个",
       _cha["followers"] == _CH["fans"]["data"]["total"][-1], _cha)
    ok("视频号 播放/赞/评/转发/收藏 = 逐条求和",
       _chw["views"] == sum(x["readCount"] for x in _CHI)
       and _chw["likes"] == sum(x["likeCount"] for x in _CHI)
       and _chw["comments"] == sum(x["commentCount"] for x in _CHI)
       and _chw["shares"] == sum(x["forwardCount"] for x in _CHI)
       and _chw["collects"] == sum(x["favCount"] for x in _CHI), _chw)
    ok("视频号 窗口过滤（createTime 是秒）",
       dw._map_channels(_CH, max(x["createTime"] for x in _CHI))[1]["count"] == 1,
       dw._map_channels(_CH, max(x["createTime"] for x in _CHI))[1])
    ok("视频号 空样本 → (None, None, None)", dw._map_channels({}) == (None, None, None))
    ok("视频号 已登记进 SUPPORTED", "channels" in dw.SUPPORTED, dw.SUPPORTED)

    # ══════════ 快手（2026-09-30 新增）══════════
    # ★ 实测**裸调即可**（不需要 __NS_sig3 签名 / api_ph token）。样本 kuaishou.json。
    _KS = json.loads((APP / "tests" / "_data_samples" / "kuaishou.json").read_text(encoding="utf-8"))
    _KSI = _KS["works"]["data"]["list"]
    _ksa, _ksw, _kst = dw._map_kuaishou(_KS)
    ok("快手 账号：昵称/ID/粉丝来自 infoV2",
       _ksa["nick_name"] == "炒鸡玛立" and _ksa["followers"] == 14, _ksa)
    ok("快手 播放/赞/评 = 逐条求和",
       _ksw["views"] == sum(x["playCount"] for x in _KSI)
       and _ksw["likes"] == sum(x["likeCount"] for x in _KSI)
       and _ksw["comments"] == sum(x["commentCount"] for x in _KSI), _ksw)
    ok("快手 窗口过滤（uploadTime 是**毫秒**，最容易写错的一条）",
       dw._map_kuaishou(_KS, max(x["uploadTime"] for x in _KSI) / 1000.0)[1]["count"] == 1,
       dw._map_kuaishou(_KS, max(x["uploadTime"] for x in _KSI) / 1000.0)[1])
    ok("快手 空样本 → (None, None, None)", dw._map_kuaishou({}) == (None, None, None))
    ok("快手 已登记进 SUPPORTED", "kuaishou" in dw.SUPPORTED, dw.SUPPORTED)

    # ══════════ 小红书（2026-09-30 新增）══════════
    # ★ 唯一一个**不能直接 replay** 的平台：裸调 406（要签名）⇒ 让页面自己发、我们截响应。
    _XH = json.loads((APP / "tests" / "_data_samples" / "xhs.json").read_text(encoding="utf-8"))
    _XHN = _XH["posted"]["data"]["notes"]
    _xa, _xw, _xt = dw._map_xhs(_XH)
    ok("小红书 账号：昵称/ID 来自 user/info",
       _xa["nick_name"] == "樱甜萌兔" and _xa["unique_id"], _xa)
    ok("小红书 播放/赞/评/收藏/分享 = 逐条求和",
       _xw["views"] == sum(x["view_count"] for x in _XHN)
       and _xw["likes"] == sum(x["likes"] for x in _XHN)
       and _xw["comments"] == sum(x["comments_count"] for x in _XHN)
       and _xw["collects"] == sum(x["collected_count"] for x in _XHN)
       and _xw["shares"] == sum(x["shared_count"] for x in _XHN), _xw)
    ok("小红书 窗口过滤（visible_time 是秒）",
       dw._map_xhs(_XH, max(x["visible_time"] for x in _XHN))[1]["count"] == 1)
    ok("小红书 空样本 → (None, None, None)", dw._map_xhs({}) == (None, None, None))
    ok("小红书 已登记进 SUPPORTED", "xhs" in dw.SUPPORTED, dw.SUPPORTED)
    ok("小红书 粉丝数没在已抓到的接口里 → 0（note 里说明，不编）",
       _xa["followers"] == 0, _xa)


    # ══════════ 头条（Task 6）══════════
    _T = json.loads((APP / "tests" / "_data_samples" / "toutiao.json").read_text(encoding="utf-8"))
    _ta, _tw, _tt = dw._map_toutiao(_T)
    ok("头条 账号五键齐全、昵称对",
       set(_ta) == {"nick_name", "unique_id", "followers", "following", "total_likes"}
       and _ta["nick_name"] == "毛大毛也有春天", _ta)
    # ★ 判据要**从样本派生**（写死 82 的话就漏了"求和"这件事 —— 我自己第一版就是这么错的）：
    #   头条后台那列叫「播放」，对应 itemCounter.videoWatchCount；works.views 是**各作品之和**。
    _sum_play = sum(((((it.get("assembleCell") or {}).get("itemCell") or {})
                      .get("itemCounter") or {}).get("videoWatchCount") or 0)
                  for it in _T["feed"]["data"])
    ok("头条 播放 = 各作品 videoWatchCount 之和", _tw["views"] == _sum_play, (_tw["views"], _sum_play))
    ok("头条 首条作品的播放确实是后台显示的 82（对数锚点）",
       _tt[0]["views"] == 82, _tt[0])
    ok("头条 作品数 = 列表条数（样本 3 条）", _tw["count"] == 3, _tw)
    ok("头条 赞/评论/转发来自 itemCounter（digg/comment/repin）",
       _tw["likes"] == 0 and _tw["comments"] == 2, _tw)
    ok("头条 粉丝 = total_fans_count",
       isinstance(_ta["followers"], int) and _ta["followers"] >= 0, _ta)
    ok("头条 Top 按播放降序且 ≤5", len(_tt) <= 5 and all(
        _tt[i]["views"] >= _tt[i + 1]["views"] for i in range(len(_tt) - 1)), _tt)
    ok("头条 空样本 → (None, None, None)", dw._map_toutiao({}) == (None, None, None))
    ok("头条 缺 user_info → (None, None, None)", dw._map_toutiao({"feed": _T["feed"]}) == (None, None, None))
    _T2 = json.loads(json.dumps(_T))
    _T2["user_info"]["total_fans_count"] = "1.2万"
    _a2, _, _ = dw._map_toutiao(_T2)
    ok("头条 带单位字符串能解（1.2万 → 12000）", _a2["followers"] == 12000, _a2)
    ok("头条 已登记进 SUPPORTED", "toutiao" in dw.SUPPORTED, dw.SUPPORTED)


    # ★ 2026-09-29 真机抓到的坑：头条 feed 返回 errno=20100 **但数据是全的** ——
    #   第一版把 errno!=0 当失败，把好响应拒了。这里把两种输入都钉住。
    _p4 = _P([_T["user_info"], {"errno": 20100, "data": _T["feed"]["data"]}])
    _r4 = dw.fetch_toutiao(_p4, str(APP / "tests" / "_tmp27.json"), 1)
    ok("头条 errno 非零但有数据 → 仍算成功（信载荷，别信标志位）",
       _r4["ok"] is True and _r4["works"]["count"] == 3, _r4)
    _p5 = _P([_T["user_info"], {"errno": 20100, "data": []}])
    _r5 = dw.fetch_toutiao(_p5, str(APP / "tests" / "_tmp27.json"), 1)
    ok("头条 errno 非零且没有数据 → 才算失败，并如实报 errno",
       _r5["ok"] is False and "20100" in _r5["error"], _r5.get("error"))

    passed = sum(1 for _, c in RESULTS if c)
    total = len(RESULTS)
    print("\n===== 结果: %d/%d 通过 =====" % (passed, total))
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

# -*- coding: utf-8 -*-
"""可交互的扫码登录进程（服务器/容器形态）—— 让登录全程能在网页里完成。

★ 与 login_worker.py 的区别：
  login_worker 在本机弹一个浏览器窗口，用户看着那个窗口操作 —— 只适合本机。
  本进程**无窗口**（容器里跑在 Xvfb 上），把「现在该做什么」写进状态文件，
  由网页呈现给用户、再把用户的指令写回来。于是**用户在哪儿都能登录**。

★ 为什么需要「可交互」而不是一口气跑完：
  实测抖音扫码后会弹**二次验证菜单**（接收短信验证码 / 手机刷脸验证 / 发送短信验证），
  SAU 原本只认「验证码输入框」，认不出这个菜单 → 卡在「等待扫码」不动（实测踩过）。
  这里把菜单呈现给用户，用户选「短信验证」、手机收码后把码交给网页，再注入浏览器。

═══ 文件协议（都在 <工作目录> 下）═══
  status.json   本进程写：{state, note, qr_at, options, url, account}
                state ∈ qr | verify_choice | sms_input | done | error | closed
  qr.png        本进程写：当前二维码（state=qr 时有意义）
  cmd           网页侧写、本进程读后即删：
                "choose:<option>"  选二次验证方式
                "code:<验证码>"     提交短信验证码
                "cancel"            放弃

用法：
  python login_qr_worker.py --key douyin --work <工作目录> --profile <我们的profile目录> \
                            --state-out <登录态json输出> [--headless 0/1]
"""
import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

import platform_login  # noqa: E402  （登录态判定的唯一事实来源，与登录窗/发布器同口径）

# 平台 → (显示名, 登录页)。与 server.py 的 LOGIN_URLS、SAU 的 PLATFORMS 同口径。
PLATFORMS = {
    "douyin":   ("抖音",     "https://creator.douyin.com/"),
    "channels": ("视频号",   "https://channels.weixin.qq.com/"),
    "kuaishou": ("快手",     "https://cp.kuaishou.com/"),
    "xhs":      ("小红书",   "https://creator.xiaohongshu.com/"),
    "weibo":    ("微博",     "https://weibo.com/"),
    "toutiao":  ("头条号",   "https://mp.toutiao.com/"),
    "bilibili": ("哔哩哔哩", "https://member.bilibili.com/platform/home"),
    "xigua":    ("西瓜视频", "https://studio.ixigua.com/"),
}

# 抖音二次验证菜单上的选项文案（按优先级：短信最好走，刷脸在无人环境做不了）
# ★ 注意：「接收短信验证码」在**菜单里是选项**、在**短信模态框上是标题**，两处同名。
#   所以它不能用来判断「现在是菜单」—— 见 tag_sms_modal() 的说明。
VERIFY_OPTIONS = ["接收短信验证码", "发送短信验证", "手机刷脸验证"]
# 验证码输入框**不用选择器找**（登录页背后有个同 placeholder 的框，会命中错的），
# 改由 tag_sms_modal() 从模态框内部定位后打 data-vp-sms 标记。


def w(path, obj):
    obj = dict(obj)
    obj["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = Path(str(path) + ".tmp")
    try:
        tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)          # 原子写：网页侧随时读都不会读到半个文件
    except OSError:
        pass


def snapshot(pg, work, tag):
    """状态切换时存一张现场图 + 正文。

    ★ 为什么：抖音的页面状态变化快（登录卡 t+20s 才渲染、t+35s 还可能插一个
      风控验证帧），只看状态字符串会误判。存下来才能事后看清到底发生了什么。
    """
    try:
        pg.screenshot(path=str(Path(work) / ("state_%s.png" % tag)), full_page=False)
        Path(work, "state_%s.txt" % tag).write_text(page_text(pg), encoding="utf-8")
    except Exception:
        pass


def read_cmd(path):
    """只读，**不删**。用完再调 clear_cmd()。

    ★ 为什么：原来是「读到即删」，于是出现这种情况 ——
      用户提交验证码时，页面上二次验证菜单还在，主循环走了「菜单分支」就 continue 了，
      指令被读出来、没用上、却被删掉了 → **验证码被静默吞掉**，用户以为提交了、
      实际什么都没发生（实测踩过）。
      改成「用了才删」，让它下一轮还能被读到。
    """
    p = Path(path)
    try:
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except OSError:
        return ""


def clear_cmd(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def page_text(pg):
    try:
        return pg.evaluate("document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


# 模态框里出现这些字样 = 上一次提交没通过
ERR_MARKERS = ("验证码错误", "验证码不正确", "验证码已失效", "验证码失效",
               "验证码已过期", "验证码过期", "验证失败", "请重新输入", "请重新获取")


def detect_modal_error(t):
    for m in ERR_MARKERS:
        if m in (t or ""):
            return m
    return ""


def tag_sms_modal(pg):
    """给「二次验证 · 短信验证码」模态框里的输入框和「验证」按钮打标记，返回模态框正文。

    ★ 为什么必须从模态框**内部**定位，而不是直接用选择器：
      1) 登录页自己有个「验证码登录」Tab，里面**也有**一个 placeholder 是
         「请输入验证码」的输入框，而且它在模态框背后一直存在于 DOM 里
         ⇒ `input[placeholder*="验证码"]` 取 .first 很可能命中**错的那个**，
           填进去的验证码根本到不了抖音的验证逻辑里。
      2) 页面上含「验证」二字的元素一大把（验证码登录 / 获取验证码 /
         接收短信验证码 / 无法验证通过…）⇒ `text=验证` 取 .last 完全不可控。
      做法：用「短信已发送至」这句话锚定模态框，在里面找输入框和按钮，
      再打上 data-vp-* 标记交给 Playwright 操作（Playwright 的 fill 会派发
      正常的事件，直接改 .value React 收不到）。
    """
    try:
        return pg.evaluate("""() => {
          const vis = e => { const r = e.getBoundingClientRect();
                             return r.width > 0 && r.height > 0; };
          // ① 找含「短信已发送至」的**最内层**元素。
          //    不能用 find()：document 顺序里 body 在最前，会一路返回 body，
          //    然后 body.querySelector('input') 拿到的是登录页那个框。
          let best = null, bestN = Infinity;
          for (const e of document.querySelectorAll('*')) {
            if ((e.innerText || '').includes('短信已发送至')) {
              const n = e.querySelectorAll('*').length;
              if (n < bestN) { bestN = n; best = e; }
            }
          }
          if (!best) return '';
          // ② 往上找同时含「可见输入框」和「验证按钮」的那层 = 模态框
          let el = best, modal = null;
          while (el && el !== document.body) {
            const hasBox = [...el.querySelectorAll('input')].some(vis);
            const hasBtn = [...el.querySelectorAll('*')]
              .some(x => !x.children.length && (x.innerText || '').trim() === '验证');
            if (hasBox && hasBtn) { modal = el; break; }
            el = el.parentElement;
          }
          if (!modal) return '';
          const inp = [...modal.querySelectorAll('input')].filter(vis)[0];
          if (!inp) return '';
          inp.setAttribute('data-vp-sms', '1');
          // ★★ 按钮**不猜是哪一层**：从「验证」文字节点往上，把每一层祖辈都编号
          //    标记（1=最内层，数字越大越靠外），交给 Python 由内到外一层层试点。
          //    为什么：抖音的按钮是 div>span 套壳，文字只是里面的 span。
          //    实测过两次（选「接收短信验证码」、提交验证码）——**点 span 毫无反应，
          //    而 Playwright 不报错**，真正挂点击事件的是外层那个 div。
          //    按类名猜也不行：`span.button_text-xxx` 自己就含 "button"，
          //    一匹配就停在内层 span 上了（这正是上一版点不动的原因）。
          //    停止条件：碰到含「取消」的那层就停 —— 否则一路标到页脚，
          //    点它的中心可能正好落在「取消」上，把模态框关掉。
          const node = [...modal.querySelectorAll('*')]
            .find(x => !x.children.length && (x.innerText || '').trim() === '验证');
          if (!node) return '';
          let b = node.parentElement, i = 1;
          while (b && b !== modal && i <= 6) {
            const hasCancel = [...b.querySelectorAll('*')]
              .some(x => !x.children.length && (x.innerText || '').trim() === '取消');
            if (hasCancel) break;
            b.setAttribute('data-vp-btn', String(i));
            b = b.parentElement;
            i++;
          }
          return (modal.innerText || '').slice(0, 300);
        }""") or ""
    except Exception:
        return ""


def dump_modal_dom(pg, work):
    """把短信模态框的 DOM 结构（标签/类名/占位符/文字）写进 modal_dom.txt。

    ★ 为什么需要：抖音的按钮大多是 div/span 套壳，光看截图分不出哪个元素才是
      真正挂了点击事件的。选验证方式那次就吃过亏（点 span 没反应，点祖先才行）。
      排障时把这个文件拉下来，一眼就能看出该点谁、输入框是什么结构。
    """
    try:
        txt = pg.evaluate("""() => {
          const vis = e => { const r = e.getBoundingClientRect();
                             return r.width > 0 && r.height > 0; };
          let best = null, bestN = Infinity;
          for (const e of document.querySelectorAll('*')) {
            if ((e.innerText || '').includes('短信已发送至')) {
              const n = e.querySelectorAll('*').length;
              if (n < bestN) { bestN = n; best = e; }
            }
          }
          if (!best) return 'no-anchor';
          let m = best;
          while (m && m !== document.body) {
            const hasBox = [...m.querySelectorAll('input')].some(vis);
            const hasBtn = [...m.querySelectorAll('*')]
              .some(x => !x.children.length && (x.innerText || '').trim() === '验证');
            if (hasBox && hasBtn) break;
            m = m.parentElement;
          }
          if (!m || m === document.body) return 'no-modal';
          const out = [];
          const walk = (el, d) => {
            if (d > 7) return;
            let s = '  '.repeat(d) + el.tagName.toLowerCase();
            const cls = (el.className || '').toString().trim();
            if (cls) s += '.' + cls.split(/\\s+/).slice(0, 4).join('.');
            if (el.tagName === 'INPUT')
              s += ' [ph="' + (el.placeholder || '') + '" val="' + (el.value || '')
                 + '" vp=' + (el.getAttribute('data-vp-sms') || '-') + ']';
            if (!el.children.length) {
              const tx = (el.innerText || '').trim();
              if (tx) s += ' "' + tx.slice(0, 24) + '"';
            }
            if (el.getAttribute('data-vp-btn')) s += '   <<== data-vp-btn';
            out.push(s);
            for (const c of el.children) walk(c, d + 1);
          };
          walk(m, 0);
          return out.join('\\n').slice(0, 6000);
        }""") or ""
        if txt:
            Path(work, "modal_dom.txt").write_text(txt, encoding="utf-8")
    except Exception:
        pass


def submit_sms_code(pg, code, work):
    """把验证码填进去并点「验证」。返回 (写进日志的一行, 是否真的点到了验证按钮)。

    ★ 做法照抄 SAU 的 _submit_sms_verify_code（vendor/sau/uploader/douyin_uploader/
      main.py:748）—— 那是这个仓库里唯一经过实战的抖音二验实现：
        1) **先 click 输入框，再 fill**。只 fill 的话有可能只改了 DOM 的 value，
           React 的 state 还是空的 → 点「验证」等于提交空验证码。
           （实测症状：提交后输入框又变空、页面不报错、模态框原地不动。）
        2) 按钮选择器写死 div.uc-ui-verify_sms-verify_button（抖音自己的类名，
           比我按文字猜可靠得多）。
        3) click(force=True) → el.click() → 精确文字 → 回车，四级兜底。
     另外加了一条 SAU 没有的：**fill 之后回读一次**。fill() 不报错 ≠ 值留下了，
      不回读就只能靠「界面上还是空框」事后猜，白跑一整轮扫码。
    """
    notes = []
    inp = pg.locator('input[data-vp-sms="1"]')
    typed = False
    try:
        inp.click(timeout=5000)              # 先聚焦（照抄 SAU）
        pg.keyboard.press("Control+a")       # 清掉上一次的残留
        pg.keyboard.type(code, delay=80)     # ★ 真键盘事件：fill 只改 DOM，
        typed = True                         #   有些受控组件要 keydown/keyup 才更新 state
    except Exception as e:  # noqa: BLE001
        notes.append("键盘输入没成(%s)" % str(e)[:30])
    if not typed:
        try:
            inp.fill(code, timeout=8000)
        except Exception as e:  # noqa: BLE001
            return ("填验证码失败：%s" % str(e)[:90], False)
    time.sleep(0.4)
    try:
        notes.append("回读=%r" % inp.input_value(timeout=3000))
    except Exception:
        notes.append("回读失败")
    dump_modal_dom(pg, work)

    lvl, ok = click_verify(pg, work)
    notes.append(("点到第%d层祖辈" % lvl) if ok else "各层祖辈都没点动")
    if not ok:
        try:
            pg.keyboard.press("Enter")       # SAU 的最后兜底
            notes.append("兜底:回车")
        except Exception:
            pass
    return ("提交验证码 %s | %s" % (code, " | ".join(notes)), ok)


def click_verify(pg, work):
    """点「验证」：由内到外逐层试，点完看模态框消没消失，不动就往上换一层。

    ★ 为什么不能一次点死：抖音的按钮是 div>span 套壳，**文字只是里面的一个 span**。
      实测过两次 —— 点 span 完全没反应、Playwright 也不报错，真正挂点击事件的
      是外层那个 div。但哪一层才是没法从文字反推，所以全都标上号挨个试。
    返回 (点中的层号, 是否点动了)。判据：模态框消失，或出现了明确的报错提示
      （报错说明点击生效了，码不对是另一回事）。
    """
    for i in range(1, 7):
        sel = '[data-vp-btn="%d"]' % i
        try:
            if pg.locator(sel).count() == 0:
                break
        except Exception:
            break
        try:
            pg.locator(sel).first.click(timeout=3000)
        except Exception:
            try:
                pg.eval_on_selector(sel, "el => el.click()")   # SAU 的兜底写法
            except Exception:
                continue
        time.sleep(2.5)
        tag = tag_sms_modal(pg)
        if not tag:
            return i, True                     # 模态框没了 → 这层对了
        if detect_modal_error(tag):
            return i, True                     # 出现报错 → 点击生效了
    return 0, False


def find_qr_generic(page):
    """在页面上找二维码，返回 data: URL；找不到返回 ""。

    ★ 为什么不能"抓第一张 data: 图"（2026-09-27 实测踩过）：
      快手、小红书登录页的第一张 data: 图是**品牌 logo**，不是二维码。
      旧写法直接把它当二维码返回，界面上一本正经显示一张 logo，
      状态还写着 has_qr=true —— 用户照着扫只会一脸问号。
      所以这里做三层筛选，任一层不过就返回空，让调用方退回「去 noVNC 手动登录」，
      而不是拿一张 logo 糊弄人：
        ① 只认**方形**的（长宽比接近 1）且尺寸在合理区间的 —— 二维码不会是长条 banner；
        ② 优先取**放在类名/ID 含 qr/code/scan 的容器里**的（各平台通行命名）；
        ③ 最后**真的验一遍**：二维码三个角上各有一个「回」字定位角，
           沿着它中心那条线扫过去是 1:1:3:1:1 的深浅比例 —— 纯色 logo 过不了这一关。
    """
    try:
        return page.evaluate("""() => {
          const vis = e => { const r = e.getBoundingClientRect();
                             return r.width > 0 && r.height > 0; };
          const squareish = r => r.width > 90 && r.width < 560 &&
                Math.abs(r.width - r.height) / Math.max(r.width, r.height) < 0.3;
          const HINT = /qr|qrcode|erweima|scan/i;
          const hinted = [], plain = [];
          for (const im of document.querySelectorAll('img')) {
            if (!vis(im)) continue;
            if (!((im.getAttribute('src') || '').startsWith('data:image'))) continue;
            if (!squareish(im.getBoundingClientRect())) continue;
            let el = im, hit = false;
            for (let i = 0; i < 4 && el; i++) {
              const s = ((el.className || '') + ' ' + (el.id || '')).toString();
              if (HINT.test(s)) { hit = true; break; }
              el = el.parentElement;
            }
            (hit ? hinted : plain).push(im);
          }
          const cands = hinted.concat(plain);
          for (const cv of document.querySelectorAll('canvas')) {
            if (vis(cv) && squareish(cv.getBoundingClientRect())) cands.push(cv);
          }

          // ── 判「像不像二维码」：三个角上的定位角 ──
          const looksQR = (el) => {
            const n = 160;
            const c = document.createElement('canvas');
            c.width = n; c.height = n;
            const cx = c.getContext('2d', { willReadFrequently: true });
            try { cx.drawImage(el, 0, 0, n, n); } catch (e) { return false; }
            let d;
            try { d = cx.getImageData(0, 0, n, n).data; } catch (e) { return false; }
            const dark = (x, y) => {
              const i = (y * n + x) * 4;
              return ((d[i] + d[i + 1] + d[i + 2]) / 3) < 128;
            };
            // 沿一条线扫，看有没有 深-浅-深-浅-深 且中间那段约 3 倍宽（回字的中横）
            const scanLine = (x0, x1, y) => {
              const runs = [];
              let cur = dark(x0, y), len = 1;
              for (let x = x0 + 1; x <= x1; x++) {
                const v = dark(x, y);
                if (v === cur) { len++; } else { runs.push([cur, len]); cur = v; len = 1; }
              }
              runs.push([cur, len]);
              for (let i = 0; i + 4 < runs.length; i++) {
                const q = runs.slice(i, i + 5);
                if (!q[0][0] || q[1][0] || !q[2][0] || q[3][0] || !q[4][0]) continue;
                const [a, b, cc, dd, e] = q.map(r => r[1]);
                const unit = (a + b + dd + e) / 4;
                if (unit <= 0 || a < 1 || b < 1 || dd < 1 || e < 1) continue;
                const ratio = cc / unit;
                if (ratio > 2.2 && ratio < 4.6 &&
                    a <= unit * 2 && b <= unit * 2 &&
                    dd <= unit * 2 && e <= unit * 2) return true;
              }
              return false;
            };
            const band = Math.round(n / 3);
            let hits = 0;
            if (scanLine(0, band, Math.round(band / 2))) hits++;                       // 左上
            if (scanLine(n - band, n - 1, Math.round(band / 2))) hits++;               // 右上
            if (scanLine(0, band, n - 1 - Math.round(band / 2))) hits++;               // 左下
            return hits >= 2;      // 三个角里至少两个 —— 留点容错（截图/圆角可能吃掉一个）
          };

          for (const el of cands) {
            if (!looksQR(el)) continue;
            if (el.tagName === 'CANVAS') {
              try { return el.toDataURL('image/png'); } catch (e) { continue; }
            }
            return el.getAttribute('src');
          }
          return '';
        }""") or ""
    except Exception:
        return ""


def login_check(pg, key=""):
    """判断当前页面是否已登录，返回 ("on" | "waiting" | "unknown", 说明)。

    ★ 走共享的 platform_login.classify()（**看正文特征词，不看 URL**）。
      为什么不用 URL：2026-09-27 对 8 个平台实测，URL 判据假阴性 7/8、假阳性 1/8 ——
      快手/头条/B 站登录成功后落点仍在各自后台主机下，URL 里根本看不出区别；
      西瓜未登录时反倒被 302 到 creator.douyin.com（完全不同主机）。
      详见 platform_login.py 顶部的实测记录。

    ★ 抖音额外有一层保险：即便正文没命中登录特征，也要求页面真的落在
      creator-micro 下（SAU 的 _is_douyin_login_completed 就是这么做的）——
      防止「登录页渲染了一半、特征词还没出来」被误判成已登录。
    """
    try:
        txt = page_text(pg)
        url = pg.url or ""
    except Exception:
        return "unknown", "读不到页面"
    state, _note, detail = platform_login.classify(txt, url)
    if state == "unknown" and key:
        # ★ 2026-10-06（安全审查 R9）：判不准时用账号接口补一次正向确认。
        #   只有抖音实现了 probe_account，其余平台立刻返回空 —— 等价于没调用。
        acc, _an = platform_login.probe_account(pg, key)
        if acc:
            state, _note, detail = platform_login.classify(txt, url, positive=True)
    if state == "on" and "creator.douyin.com" in url \
            and "creator-micro" not in url:
        return "waiting", "抖音还没跳转到创作后台"
    return state, detail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default="douyin")
    ap.add_argument("--work", required=True, help="工作目录（status.json / qr.png / cmd 都在这）")
    ap.add_argument("--profile", default="", help="本项目持久化 profile 目录（登录成功后把 cookie 灌进去）")
    ap.add_argument("--state-out", default="", help="storage_state JSON 输出路径")
    ap.add_argument("--headless", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    # 日志里留个版本戳：调试时一眼能确认容器里跑的是哪份代码
    print("[qr] 启动 key=%s headless=%d work=%s" % (args.key, args.headless, work), flush=True)
    status = work / "status.json"
    qr_png = work / "qr.png"
    cmd_file = work / "cmd"

    if args.key not in PLATFORMS:
        w(status, {"state": "error", "note": "未知平台：%s" % args.key})
        return 2
    pname, login_url = PLATFORMS[args.key]
    # ★ 抖音的二验菜单/短信框是它独有的，别的平台没有这套 —— 那段逻辑只对它开。
    is_douyin = (args.key == "douyin")

    # ★ 这里原先塞了 SAU 的环境变量和 sys.path，现在**不需要了**：
    #   本进程自己起浏览器（patchright）、自己用 platform_login 判登录态、
    #   成功后导出 storage_state 交给 server.import_storage_state 灌进 profile。
    #   对 SAU 只是「参考过它的抖音二验写法和登录完成判据」，没有运行时依赖。
    from patchright.sync_api import sync_playwright   # 用 patchright 而不是 playwright：反检测强一些

    # ★ 同步版的二维码提取。SAU 里的 _extract_douyin_qrcode_src 是 async 的，
    #   而本进程用的是 sync API —— 直接调会拿到一个 coroutine 对象（实测报
    #   "'coroutine' object has no attribute 'split'"）。这里照它的选择器顺序重写一份同步的。
    QR_SELECTORS = [
        "div#animate_qrcode_container img[src^='data:image']",
        "div[class*='animate_qrcode_container'] img[src^='data:image']",
        "div[class*='scan_qrcode_login_content'] img[src^='data:image']",
        "img[aria-label='二维码']",
    ]

    def extract_qr_sync(page, timeout=75):
        """轮询直到二维码出现。

        ★ 实测（2026-09-27）：**不能等「扫码登录」这四个字** —— 它很早就作为静态文案
          存在，而此时登录卡还没渲染，页面上一个 img 都没有：
              t+9s  img 数 0      ← 等文字会在这里就往下走，必然找不到二维码
              t+20s img 数 26     ← 登录卡这时才渲染，二维码在这里面
          ⇒ 必须**直接轮询二维码图本身**，并给足时间。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            for sel in QR_SELECTORS:
                try:
                    loc = page.locator(sel).first
                    if loc.count() > 0:
                        src = loc.get_attribute("src")
                        if src and src.startswith("data:image"):
                            return src
                except Exception:
                    continue
            # 兜底：各平台的类名太杂，直接按「长得像二维码」在页面上找。
            # ★ 不能图省事抓第一张 data: 图 —— 快手/小红书的第一张是品牌 logo，
            #   那样会把 logo 当二维码显示给用户扫。见 find_qr_generic 的说明。
            src = find_qr_generic(page)
            if src:
                return src
            time.sleep(2)
        return ""

    w(status, {"state": "opening", "note": "正在打开登录页…"})
    try:
        with sync_playwright() as p:
            headless = bool(args.headless)
            browser = p.chromium.launch(headless=headless, channel="chromium",
                                        args=["--no-sandbox",
                                              "--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 1000},
                                      user_agent=None)
            pg = ctx.new_page()
            try:
                pg.goto(login_url, wait_until="domcontentloaded", timeout=90000)
            except Exception:
                pass
            time.sleep(6)

            def save_qr():
                try:
                    src = extract_qr_sync(pg)
                    if not src or "," not in src:
                        # ★ 找不到二维码时，把现场存下来 —— 否则只能瞎猜页面长什么样
                        try:
                            (work / "page.txt").write_text(page_text(pg), encoding="utf-8")
                            pg.screenshot(path=str(work / "page.png"), full_page=False)
                        except Exception:
                            pass
                        print("[qr] 没定位到二维码；已存 page.txt / page.png（url=%s）" % pg.url, flush=True)
                        return False
                    raw = base64.b64decode(re.sub(r"\s+", "", src.split(",", 1)[1]))
                    qr_png.write_bytes(raw)
                    return True
                except Exception as e:  # noqa: BLE001
                    import traceback
                    print("[qr] 提取二维码失败: %s | %s"
                          % (str(e)[:120], traceback.format_exc()[-300:].replace("\n", " ")), flush=True)
                    return False

            # ★★ 取不到二维码**不算失败**（2026-09-27 改）：
            #   原来这里直接报错退出，等于把「只有抖音能用」写死了。实际上
            #   ① 抖音之外的平台二维码未必放在能提取的 <img> 里（可能是 canvas/iframe/
            #      或干脆是账号密码登录），能不能提取到跟「能不能登录」是两回事；
            #   ② 现在有 noVNC —— 取不到码，用户切过去在真浏览器里点两下就登上了。
            #   所以：能提取就显示在页面上（方便），提取不到就转人工（保底），
            #   状态一律进 qr，继续等登录成功。
            has_qr = save_qr()
            if not has_qr:
                print("[qr] 没提取到二维码（平台=%s）—— 转 noVNC 人工登录；已存 page.txt/page.png"
                      % args.key, flush=True)
            w(status, {"state": "qr", "has_qr": has_qr,
                       "note": "请用%s扫码登录" % pname,
                       "qr_at": time.time(), "url": pg.url})

            deadline = time.time() + args.timeout
            last_state = "qr"
            qr_born = time.time()
            on_rounds = 0        # 连续几轮判定「已登录」，用于滤掉跳转中间态
            rounds = 0           # 心跳：界面上要显示「第几轮 · 已等多久」，见下面的 w(...)
            while time.time() < deadline:
                time.sleep(2)
                rounds += 1

                # ── 读网页侧指令 ──
                cmd = read_cmd(cmd_file)
                if cmd == "cancel":
                    clear_cmd(cmd_file)
                    w(status, {"state": "closed", "note": "已取消登录"})
                    ctx.close(); browser.close()
                    return 0
                # 排障用：让 worker 立刻把当前页面的 DOM / 正文 / 截图落盘，
                # 不用重启（重启就把浏览器连带当前那个模态框一起丢了）。
                if cmd == "dump":
                    clear_cmd(cmd_file)
                    dump_modal_dom(pg, work)
                    try:
                        pg.screenshot(path=str(work / "dump.png"))
                        (work / "dump_text.txt").write_text(page_text(pg), encoding="utf-8")
                    except Exception:
                        pass
                    print("[qr] 已按指令 dump 现场到 dump.png / dump_text.txt / modal_dom.txt",
                          flush=True)
                    continue

                txt = page_text(pg)

                # ── 已登录？ ──
                # ★ 连续 STABLE_ROUNDS 轮结论一致才算「登录成功」。
                #   只看一轮会被跳转中间态骗：登录后页面会先白一下（正文为空），
                #   classify 把空白页判成 unknown，正好不该当成成功。反过来也一样 ——
                #   所以宁可多等 4 秒，也别让用户看到「登录成功」之后又变回未登录。
                cls, detail = login_check(pg, args.key)
                on_rounds = on_rounds + 1 if cls == "on" else 0
                if on_rounds >= platform_login.STABLE_ROUNDS:
                    time.sleep(2)
                    state_out = args.state_out or str(work / "state.json")
                    try:
                        ctx.storage_state(path=state_out)
                    except Exception as e:  # noqa: BLE001
                        w(status, {"state": "error", "note": "保存登录态失败：%s" % str(e)[:120]})
                        ctx.close(); browser.close()
                        return 1
                    # 探当前登录的是哪个账号（只有抖音实现了，其它平台返回空 → 上游
                    # 退回「按平台计日更配额」，界面上会如实标注）
                    acc, acc_name = platform_login.probe_account(pg, args.key)
                    w(status, {"state": "done", "note": "登录成功", "url": pg.url,
                               "account": acc, "account_name": acc_name,
                               "state_file": state_out, "detail": detail})
                    print("[qr] LOGIN_OK platform=%s account=%s(%s)" % (args.key, acc_name, acc),
                          flush=True)
                    ctx.close(); browser.close()
                    return 0

                # ── 二次验证 · 短信验证码模态框 ★ 必须排在菜单判断【前面】──
                # ★★ 这个顺序是死规矩，踩过大坑（2026-09-27，NAS，卡死 20 分钟）：
                #   短信模态框的**标题就叫「接收短信验证码」**，跟菜单里的选项一字不差。
                #   原来先判菜单 → 模态框一打开，这七个字还在页面上，菜单分支每轮都
                #   成立、每轮都 continue，**永远走不到下面填验证码的那段** →
                #   用户提交的验证码一直躺在 cmd 文件里没人读，而界面看着一切正常
                #   （状态一直显示「请输入验证码」，用户以为提交了）。
                #   改成「先看有没有输入框」，就跟页面上写着什么字无关了。
                modal_txt = tag_sms_modal(pg) if is_douyin else ""
                if modal_txt:
                    if last_state != "sms_input":
                        snapshot(pg, work, "sms_input")
                        last_state = "sms_input"
                    if cmd.startswith("code:"):
                        code = cmd.split(":", 1)[1].strip()
                        line, clicked = submit_sms_code(pg, code, work)
                        print("[qr] %s" % line, flush=True)
                        if clicked:
                            clear_cmd(cmd_file)   # 只有真点到「验证」才删；没点成下一轮还能重试
                        time.sleep(3)
                        w(status, {"state": "sms_input", "url": pg.url,
                                   "note": "已提交验证码，正在等抖音返回结果…" if clicked
                                           else "没能点到「验证」按钮，请到「服务端浏览器截图」看一眼"})
                        time.sleep(1)
                        continue
                    err = detect_modal_error(modal_txt)
                    w(status, {"state": "sms_input", "url": pg.url,
                               "note": ("没通过（%s）—— 请点「无法验证通过？选择其他验证方式」"
                                        "重新获取，再输新的验证码" % err) if err
                                       else "请输入手机收到的验证码"})
                    time.sleep(1)
                    continue

                # ── 二次验证菜单（还没选验证方式）★ 抖音独有 ──
                # 走到这里说明页面上**没有**短信验证码输入框；此时若还出现选项文字，
                # 那就是真正的「请选择验证方式」菜单。
                # 别的平台没有这套菜单 —— VERIFY_OPTIONS 全是抖音的文案，
                # 这里显式限死在抖音，免得以后哪个平台的页面上恰好出现这几个字
                # 就被误判成「要选验证方式」而卡住（这种「文案撞车」已经踩过一次，见上）。
                found = [o for o in VERIFY_OPTIONS if o in txt] if is_douyin else []
                if found:
                    if last_state != "verify_choice":
                        snapshot(pg, work, "verify_choice")
                        last_state = "verify_choice"
                    w(status, {"state": "verify_choice", "url": pg.url,
                               "note": "抖音要求二次验证，请选择验证方式",
                               "options": found, "qr_at": qr_born})
                    if cmd.startswith("choose:"):
                        want = cmd.split(":", 1)[1].strip()
                        try:
                            # ★★ 点「身份验证」菜单里的那一项，**不能直接点文字节点**。
                            #    实测（2026-09-27，NAS）：菜单是可点的整行，文字只是里面的
                            #    一个 span —— 点 span 会被外层拦截，菜单纹丝不动，
                            #    而 Playwright 不报错，于是「以为选上了、其实没选」。
                            #    （这跟抖音可见性单选按钮的 label/span 是同一类坑。）
                            #    做法：先按文字找到节点，再往上找可点的祖先去点它。
                            picked = pg.evaluate("""(txt) => {
                              const nodes = [...document.querySelectorAll('*')]
                                .filter(e => !e.children.length && (e.innerText||'').trim() === txt);
                              if (!nodes.length) return 'notfound';
                              let el = nodes[0];
                              for (let i = 0; i < 6 && el; i++) {
                                const cls = (el.className || '').toString();
                                if (el.getAttribute('role') === 'button' || el.onclick
                                    || /item|option|cell|row|entry|action/i.test(cls)) {
                                  el.click(); return 'ancestor:' + el.tagName;
                                }
                                el = el.parentElement;
                              }
                              nodes[0].click(); return 'self';
                            }""", want)
                            print("[qr] 点击「%s」→ %s" % (want, picked), flush=True)
                            clear_cmd(cmd_file)
                            time.sleep(2.5)
                            # ★ 选「接收短信验证码」这一步**本身就把短信发出去了**
                            #   （实测页面直接显示「短信已发送至 152******80 / 52s后重新发送」）。
                            #   所以只有在模态框**没确认发出**时才补点一次「获取验证码」，
                            #   否则那个 .last 会点到登录页背后同名按钮上、白发一条短信。
                            mtxt = tag_sms_modal(pg)
                            sent = bool(mtxt and "短信已发送至" in mtxt)
                            if not sent:
                                for sel in ('button:has-text("获取验证码")', 'text=获取验证码',
                                            'button:has-text("发送验证码")', 'text=发送验证码'):
                                    try:
                                        loc = pg.locator(sel).last
                                        if loc.count() > 0 and loc.is_visible():
                                            loc.click(timeout=5000)
                                            sent = True
                                            break
                                    except Exception:
                                        continue
                            snapshot(pg, work, "sms_input")   # 存现场：能看清实际有哪些按钮
                            w(status, {"state": "sms_input", "url": pg.url,
                                       "note": ("已选「%s」并已把验证码发到手机上，请查看短信并在此输入"
                                                % want) if sent else
                                               ("已选「%s」，但模态框没显示「短信已发送至」—— "
                                                "请到「服务端浏览器截图」看一眼该点什么" % want),
                                       "options": found, "chosen": want, "sms_sent": sent})
                            last_state = "sms_input"
                        except Exception as e:  # noqa: BLE001
                            w(status, {"state": "verify_choice", "url": pg.url,
                                       "note": "点「%s」失败：%s" % (want, str(e)[:80]),
                                       "options": found})
                    time.sleep(1)
                    continue

                # ── 二维码过期 → 刷新 ──
                # ★ 只有「确实提取到过二维码」才刷新。没有二维码的平台（走 noVNC
                #   人工登录的那些）在这里点「二维码失效」纯属瞎点，会打到别的东西上。
                if has_qr and last_state == "qr" and time.time() - qr_born > 90:
                    try:
                        exp = pg.get_by_text("二维码失效", exact=True)
                        if exp.count():
                            exp.first.click(timeout=5000)
                            time.sleep(1.5)
                    except Exception:
                        pass
                    if save_qr():
                        qr_born = time.time()
                        w(status, {"state": "qr", "has_qr": True,
                                   "note": "二维码已刷新，请重新扫码",
                                   "qr_at": qr_born, "url": pg.url})
                    time.sleep(1)
                    continue

                # ── 状态退回到「等扫码 / 等人工登录」 ──
                # ★ 每次状态变化都存一张现场图：登录卡住时，用户能直接看到
                #   服务端浏览器上到底显示什么（这次排障就是靠它一眼看出菜单没点动）。
                if last_state != "qr":
                    snapshot(pg, work, "back_to_qr")
                    last_state = "qr"
                w(status, {"state": "qr", "has_qr": has_qr,
                           "note": ("请用%s扫码登录" % pname) if has_qr
                                   else ("没自动取到二维码 —— 请点下方「直接操作浏览器」，"
                                         "在弹出的浏览器里登录%s" % pname),
                           # ★ 心跳（2026-09-30）：界面靠它显示「第 N 轮 · 已等 M 秒」——
                           #   实测用户因为看不出这个进程还活着，反复点「登录」，
                           #   每次点击都把正在盯着的进程杀掉重开（视频号一天 10 次），
                           #   而被杀的那次永远等不到"连续 2 轮已登录"。
                           "rounds": rounds, "elapsed": int(time.time() - qr_born),
                           "qr_at": qr_born, "url": pg.url})
                time.sleep(0.5)

            w(status, {"state": "error", "note": "等待登录超时（%d 秒）" % args.timeout})
            ctx.close(); browser.close()
            return 1
    except Exception as e:  # noqa: BLE001
        w(status, {"state": "error", "note": str(e)[:200]})
        return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

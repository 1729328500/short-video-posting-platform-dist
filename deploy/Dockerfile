# -*- mode: dockerfile -*-
# 短视频发布台 · 容器镜像
#
# 设计要点（都来自实测，不是照抄模板）：
#
#   1. **带 Xvfb**：实测抖音能识别无头浏览器 —— 无头扫码登录走不通（扫码+确认后页面
#      URL 80 秒不动）。所以容器里让浏览器**以有头模式跑在虚拟显示上**。
#      容器启动脚本先起 Xvfb，再把 DISPLAY 指过去。
#   2. **两套浏览器都装**：
#        · playwright 自带 chromium —— 本项目自己的链路（登录窗/发布/抓取）用
#        · patchright 自带 chromium —— vendored 的 social-auto-upload 用
#      ⚠️ 这里**不装 Google Chrome**，所以 SAU 里任何要 `channel="chrome"` 的地方都会炸。
#        上游绝大多数上传器用 "chromium"，唯独视频号那处原本写 "chrome" ——
#        已在 vendor/sau 里改掉（见 vendor/sau/NOTICE.md 第 5 处改动）。
#        回归测试：app/tests/test_14_sau_browser.py。
#      少一个浏览器就会有一半功能跑不起来。
#   3. **数据全在 /data 卷**：素材、数据库、登录 profile、配置。代码层可以只读，
#      更新时换镜像、数据原地不动。
#   4. **PYTHONDONTWRITEBYTECODE=1**：不往代码目录写 __pycache__，支持只读挂载。
#   5. **默认绑 0.0.0.0**（容器里必须），但应用自带守卫：没设访问口令会拒绝启动。

# ★ 基础镜像做成可覆盖：国内直连 registry-1.docker.io 基本拉不动（实测超时）。
#   构建时用 --build-arg BASE_IMAGE=<镜像源>/library/python:3.12-slim-bookworm 覆盖。
#   compose 里已经默认填了国内源，见 docker-compose.yml。
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISPLAY=:99 \
    VP_BROWSER_CHANNEL=chromium \
    TZ=Asia/Shanghai

# ── 系统依赖：Xvfb + 浏览器运行库（字体是中文渲染的关键，少了下不了手）──
#   x11vnc   —— 把 Xvfb 的虚拟显示导出来，供 noVNC 转发
#   fluxbox  —— 极轻的窗口管理器。**不能省**：没有它，chromium 的窗口没法移动/
#               聚焦，弹窗可能开在屏幕外，人在 noVNC 里就够不着了
#
# ★ apt 也要换国内源：x11vnc 会拖进一整套 X 库，走 deb.debian.org 实测慢到
#   构建卡十几分钟（缓存十几秒不涨）。bookworm 官方镜像用的是 deb822 格式的
#   sources.list.d/debian.sources，老版本用 sources.list —— 两个都 sed 一遍，
#   哪个存在改哪个。要改回官方源就把 APT_MIRROR 设成 deb.debian.org。
ARG APT_MIRROR=mirrors.tuna.tsinghua.edu.cn
RUN set -eux; \
    for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        [ -f "$f" ] && sed -i "s|deb.debian.org|${APT_MIRROR}|g" "$f" || true; \
    done; \
    apt-get update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        xvfb x11-utils x11vnc fluxbox \
        ca-certificates curl \
        fonts-noto-cjk fonts-noto-color-emoji \
        tzdata \
    && rm -rf /var/lib/apt/lists/*
# ★ Acquire::Retries 不是可选项：实测清华源上 fluxbox 那个包偶发 500/EOF，
#   不重试就整层失败、168MB 白下。（默认重试次数是 0。）

WORKDIR /app

# ── Python 依赖 ──
# playwright（本项目链路）+ patchright（SAU 链路）
# ★ 用国内镜像，服务器上直连 pypi 常常慢到超时
ARG PIP_INDEX=https://mirrors.sjtug.sjtu.edu.cn/pypi/web/simple
RUN pip install --no-cache-dir -i "$PIP_INDEX" playwright patchright

# ── 浏览器（playwright 与 patchright 共用同一份 chromium）──
# ★ 实测结论（2026-09-27）：
#   · 浏览器二进制从 **npmmirror** 拉，11.3 MB/s，**不需要代理**
#     （官方 cdn.playwright.dev 直连只有 98 KB/s，195MB 要半小时；走代理 16.9MB/s）
#   · patchright 与 playwright **共用同一个安装目录**（ms-playwright/chromium-1243），
#     所以装一次两边都能用 —— 少下 200MB。
#   · 地址格式是 /builds/cft/{chrome版本}/{平台}/chrome-{平台}.zip，
#     npmmirror 完整镜像了这个路径（旧版 /builds/chromium/{rev}/ 的格式它只有 arm64）。
# 需要走官方 CDN 时把 PW_DOWNLOAD_HOST 覆盖成 https://cdn.playwright.dev 即可。
ARG PW_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
ENV PLAYWRIGHT_DOWNLOAD_HOST=${PW_DOWNLOAD_HOST}
# --with-deps 顺带把 chromium 需要的系统库补齐
# ★ 千万别在这条后面加 `|| true`：那会把下载失败吞掉，构建照样「成功」，
#   产出一个**没有浏览器的镜像** —— 直到运行时才报 "Executable doesn't exist"
#   （实测踩过，而且排查了两轮）。宁可构建失败，也不要一个残废镜像。
RUN python -m playwright install --with-deps chromium     && python -m patchright install chromium

# noVNC 的 WebSocket 中转（x11vnc 只监听容器内，外面靠它转发）。
# ★ 故意单独一层：如果跟上面那条 playwright/patchright 写在一起，
#   以后只要动这个依赖，下面这层 195MB 的 chromium 也会跟着作废重下，构建时间翻倍。
RUN pip install --no-cache-dir -i "$PIP_INDEX" websockify

# ── noVNC 网页客户端（静态文件已 vendor 进仓库，构建不依赖外网）──
# ★ 为什么要有它：
#   抖音的二次验证弹窗（短信 / 刷脸）结构说变就变，脚本永远追不上 ——
#   vendored 的 SAU 自己遇到二验的做法就是「请在弹出的浏览器里手动输入」。
#   而登录是低频操作（登一次能用很久），与其一直猜选择器，
#   不如把容器里那台浏览器**直接搬到网页上给人操作**，登完让程序收 cookie。
COPY deploy/novnc /opt/novnc

# ── 应用代码 + 入口脚本 ──
# 构建上下文＝项目根目录（短视频发布台-源码-v1.1.0/）
COPY app/ /app/
COPY deploy/entrypoint.sh /entrypoint.sh
# ★ 去掉可能混进来的 CR：CRLF 的 shebang 会让内核找不到解释器，
#   报错是 no such file or directory —— 文件明明在，很容易看歪（实测踩过）。
RUN chmod +x /entrypoint.sh && mkdir -p /data

VOLUME ["/data"]
EXPOSE 8947 6080

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8947/api/health || exit 1

WORKDIR /app
ENTRYPOINT ["/entrypoint.sh"]

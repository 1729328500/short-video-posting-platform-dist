#!/bin/sh
# 容器入口：先起虚拟显示，再拉服务。
#
# ★ 为什么必须有 Xvfb：
#   实测（2026-09-27）**无头浏览器在抖音上扫码登录走不通** —— 用户扫码并确认后，
#   无头页面 80 秒 URL 一步没动；同日有头模式两轮内即登录成功。
#   抖音能识别无头。所以容器里让浏览器跑在 **Xvfb 虚拟显示** 上，
#   对它来说就是「有屏幕的有头环境」。
#
# ⚠️ Xvfb 能否真正骗过抖音风控，**必须在目标机上实测确认** —— 见 deploy/README.md
#    的「首次部署验证清单」。即便不行也有保底：在别的机器上扫码后把登录态同步过来。
#
# ★ 二验兜底（2026-09-27 加）：抖音扫码后必弹二次验证（短信/刷脸），弹窗 DOM 说变就变
#   —— vendored 的 SAU 里那个按钮类名 uc-ui-verify_sms-verify_button 在现网已经不存在了。
#   所以这里额外起一套 **noVNC**，把虚拟显示搬到网页上：自动流程走不通时，
#   人可以在网页里直接操作容器内那台真浏览器，程序照样负责「检测登录成功→收 cookie」。
set -e

DISPLAY_NUM="${DISPLAY_NUM:-99}"
SCREEN="${XVFB_SCREEN:-1600x1100x24}"
export DISPLAY=":${DISPLAY_NUM}"

# ★★ 先清掉上一次留下的 X 锁 —— 不清的话 restart 一次浏览器功能就永久报废（实测踩过）：
#   本脚本最后用 `exec` 把 shell 换成 server.py，而 **exec 之后 trap 就没了**，
#   所以 `docker restart` 的信号只送到 PID 1（python），Xvfb 收不到优雅退出，
#   /tmp/.X99-lock 留了下来。而 /tmp 在同一个容器里是**保留的**，于是下次启动时
#   Xvfb 直接拒绝：
#       (EE) Fatal server error: Server is already active for display 99
#   更坑的是当时**没有任何地方报错** —— 容器 healthy、接口 200，
#   只有所有浏览器功能静悄悄地全废了，排查了很久。
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

echo "[entrypoint] 启动虚拟显示 Xvfb ${DISPLAY} (${SCREEN})"
Xvfb "${DISPLAY}" -screen 0 "${SCREEN}" -nolisten tcp -ac >/tmp/xvfb.log 2>&1 &
XVFB_PID=$!

# 等 Xvfb 就绪（最多 10 秒）
# ★ 必须有失败分支：原来只有 break、没有 else，Xvfb 起不来就默默往下走，
#   留下一个「看着正常、其实什么都干不了」的容器。宁可起不来直接退出。
READY=0
i=0
while [ $i -lt 20 ]; do
    if xdpyinfo -display "${DISPLAY}" >/dev/null 2>&1; then
        echo "[entrypoint] 虚拟显示已就绪"
        READY=1
        break
    fi
    sleep 0.5
    i=$((i + 1))
done
if [ "$READY" != "1" ]; then
    echo "[entrypoint] ✗ 虚拟显示启动失败 —— 扫码登录/发布/数据抓取全都会用不了。"
    echo "[entrypoint]   下面是 Xvfb 的报错："
    cat /tmp/xvfb.log 2>/dev/null || true
    exit 3
fi

# ── noVNC：给这台「无头的有头浏览器」配一双手 ──
# 为什么值得加：登录是**低频操作**（登一次管很久），而二验弹窗是**高频变化**的东西。
# 与其一直追着抖音改选择器，不如让人点两下。程序该自动的部分（检测登录成功、
# 导出 storage_state、灌进持久化 profile）一点没少，只是多了一条人工通路。
VNC_PORT="${VNC_PORT:-5900}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
# ★ 2026-10-06（安全审查 R4）：**默认关闭**，且不再有固定回退口令。
#   原来这里是 NOVNC 默认 1 + 口令回退到一个仓库里公开的固定值 —— 默认敞开，
#   而且下面把口令打进日志。这条通路独立于应用访问口令，拿到它就能操作一个
#   已登录所有平台的浏览器。
NOVNC="${NOVNC:-0}"
X11VNC_PID=""
NOVNC_PID=""

if [ "${NOVNC}" = "1" ] && [ -z "${VNC_PASSWORD:-}" ]; then
    echo "[entrypoint] 拒绝启动：启用 noVNC 必须显式设置 VNC_PASSWORD（不提供默认口令）" >&2
    exit 4
fi

if [ "${NOVNC}" = "1" ]; then
    # 窗口管理器：没有它，chromium 的窗口挪不动、也拿不到焦点，弹窗可能开在屏幕外
    fluxbox >/tmp/fluxbox.log 2>&1 &
    echo "[entrypoint] 窗口管理器 fluxbox 已启动"

    mkdir -p /data/.vnc
    x11vnc -storepasswd "${VNC_PASSWORD}" /data/.vnc/passwd >/dev/null 2>&1

    # -localhost：x11vnc 只监听容器内的 127.0.0.1，**不直接对外**，外面一律走 websockify
    # -noxdamage：容器里 Xdamage 常常导致画面全黑，关掉最稳
    x11vnc -display "${DISPLAY}" -rfbport "${VNC_PORT}" -localhost \
           -rfbauth /data/.vnc/passwd -forever -shared -repeat -noxdamage \
           >/tmp/x11vnc.log 2>&1 &
    X11VNC_PID=$!

    sleep 1
    websockify --web /opt/novnc "${NOVNC_PORT}" "127.0.0.1:${VNC_PORT}" \
               >/tmp/websockify.log 2>&1 &
    NOVNC_PID=$!
    echo "[entrypoint] noVNC 已就绪：http://127.0.0.1:${NOVNC_PORT}/vnc.html"
    # ★ 不打印口令（2026-10-06 安全审查 R4）：容器日志会被收集、转发、留存，
    #   凭证不该进日志。口令是你自己设的环境变量，自己知道。
    echo "[entrypoint]   VNC 口令：见你设置的 VNC_PASSWORD 环境变量（不在此输出）"
else
    echo "[entrypoint] NOVNC=0 —— 跳过 noVNC，登录只能走自动流程"
fi

# Xvfb 挂了就把服务一起带走，避免留下一个「以为有显示、其实没有」的容器
trap 'echo "[entrypoint] 收到退出信号"; kill $XVFB_PID $X11VNC_PID $NOVNC_PID 2>/dev/null; exit 0' TERM INT

echo "[entrypoint] 启动服务…"
# 三个环境变量可覆盖默认值；也可在 docker run 尾部追加给 server.py 的参数。
#   HOST     默认 0.0.0.0（容器里必须对外，否则 nginx/宿主机进不来）
#   PORT     默认 8947
#   DATA_DIR 默认 /data
exec python /app/server.py \
    --host "${HOST:-0.0.0.0}" \
    --port "${PORT:-8947}" \
    --data-dir "${DATA_DIR:-/data}" \
    --no-browser "$@"

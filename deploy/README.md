# 部署到服务器 / NAS（Docker）

> 目标机器实测环境：**绿联 DXP4800 Plus**（Intel 奔腾金牌 8505 · x86_64 · UGOS Pro 基于 Linux）。
> 本项目镜像构建为 **linux/amd64**，与之一致；本机（Windows + Docker Desktop）已能构建，架构相同。

---

## 0. 先明确一件事：**部署后能达到什么、达不到什么**

| 能力 | 无人值守 | 说明 |
|---|---|---|
| 定时**抓取**数据 | ✅ | 完全自动 |
| 定时**播报**（企微/飞书） | ✅ | 完全自动 |
| **扫码登录** | ✅ 网页里点两下 | 二维码显示在页面上；二次验证用 **noVNC** 自己点（见 3.1） |
| **发布** | ⚠️ 需要人 | 抖音发布必弹**短信验证码**，只能人工输（实测结论，见《08-平台适配策略》三节） |

> ⚠️ **已知实测结论**：**无头浏览器在抖音上扫码登录走不通** —— 用户扫码并确认后，无头页面 80 秒 URL 未动；
> 同日有头模式两轮内即登录成功。所以本镜像**内置 Xvfb**，让浏览器以有头模式跑在虚拟显示上。
>
> ⚠️ **二次验证别指望脚本**（2026-09-27 定论）：扫码后抖音必弹二验菜单（短信/刷脸），
> 而这个弹窗的结构**说变就变** —— vendored 的 SAU 里写死的按钮类名 `uc-ui-verify_sms-verify_button`
> 在现网已经**完全不存在**了。SAU 自己的答案就是「请在弹出的浏览器中手动输入」。
> 所以本镜像内置 **noVNC**：把容器里那台浏览器**搬到网页上给人操作**。
> 登录是低频操作（登一次管很久），人工点两下完全可接受；程序照旧负责
> 「检测到登录成功 → 导出登录态 → 灌进持久化 profile」。

---

## 1. 目录准备（在 NAS 上）

```bash
# 在 NAS 上找个位置，例如 /volume1/docker/shipin-fabu
mkdir -p /volume1/docker/shipin-fabu
cd /volume1/docker/shipin-fabu

# 需要这些文件（从源码包拷过来）：
#   deploy/Dockerfile
#   deploy/docker-compose.yml
#   deploy/entrypoint.sh
#   deploy/nginx.conf
#   app/                     ← 整个应用目录
```

目录结构应该是：

```
shipin-fabu/
├── deploy/
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── entrypoint.sh
│   └── nginx.conf
├── app/                     ← 应用代码
└── data/                    ← 【运行后自动生成】所有数据都在这，务必备份
```

---

## 2. **必须先设访问口令**（否则起不来）

应用自带守卫：**监听 0.0.0.0 却没设口令 → 拒绝启动**（`exit 2`）。这是有意的 ——
本服务的写接口能删素材、改配置、触发真实推送，裸奔对外等于把控制权交出去。

**一条命令设好（应用内置的一次性命令）：**

```bash
docker run --rm   -v /volume1/docker/shipin-fabu/data:/data   shipin-fabu:1.7.0 --set-password '你的访问口令'
```

输出 `访问口令已设好：/data/config.json` 即可。

> ⚠️ **别用「先以本机模式起一次进去设」这个办法** —— 容器里 `HOST=127.0.0.1` 意味着
> 服务只绑**容器自己的 loopback**，端口映射根本进不去，你会以为容器坏了（实测踩过）。
> 口令哈希是 PBKDF2 加盐存的，不落明文。

然后正式启动：

```bash
cd /volume1/docker/shipin-fabu
docker compose -f deploy/docker-compose.yml up -d
```

---

## 3. 首次部署验证清单（**按顺序做，别跳**）

| # | 检查 | 命令 / 做法 | 期望 |
|---|---|---|---|
| 1 | 容器起来了 | `docker compose ps` | `Up (healthy)` |
| 2 | 健康探针 | `curl http://127.0.0.1:8947/api/health` | `{"ok":true,...}` |
| 3 | 版本正确 | 同上，看 `version` | 与镜像标签一致 |
| 4 | **Xvfb 在跑** | `docker exec shipin-fabu ps aux \| grep Xvfb` | 有 Xvfb 进程 |
| 5 | **浏览器能起** | 数据页点【立即抓取】，`docker compose logs -f app` | 日志里有浏览器启动、无 `Target page...closed` |
| 6 | **★ 扫码登录能否过风控** | 账号页 → 抖音 → 扫码登录 | **这一条是唯一的未知数，见下** |
| 7 | 抓取出真数据 | 数据页点【立即抓取】，等 1~2 分钟 | 抖音卡片出现真实播放/点赞数 |
| 8 | 播报能发出 | 设置页确认企微/飞书开关 → 数据页【立即播报】 | 企微/飞书收到消息 |

### ★ 第 6 条：扫码登录怎么做

**结果 A：能扫上、也不需要二验** → `headed + Xvfb` 这条路成立，页面里直接扫码完成。

**结果 B：弹了二次验证 / 卡住了** → 点弹层左下角的 **【打不开？直接操作浏览器】**，
切到 noVNC 里手动做完。详见下一节。

> 保底思路（已不太需要）：在 Windows 机器上扫码，再把 `app/data/browsers/` 整个
> 拷到 NAS 的 `data/browsers/` 然后重启容器。登录态 profile 是可移植的
> （`sau_bridge.export_storage_state()` 就是做 profile ↔ storage_state 互转的）。

具体做法（本项目已内置能力）：

```bash
# ① 在 Windows 机器上（本机模式）扫码登录好，确认账号页变绿
# ② 把整个登录态目录拷到 NAS：
#    Windows:  短视频发布台-源码-v1.1.0/app/data/browsers/
#    →           /volume1/docker/shipin-fabu/data/browsers/
# ③ 重启容器
docker compose -f deploy/docker-compose.yml restart
```

登录态（Chromium profile）是**可移植**的 —— 这条链路本项目已经跑通过
（`sau_bridge.export_storage_state()` 就是做 profile ↔ storage_state 互转的）。

---

## 4. 更新流程（换镜像、不动数据）

```bash
# ── 在开发机（Windows）上 ──
# ① 改代码 → 跑全量回归 → 改 docker-compose.yml 里的 image 标签（如 :1.8.0）
docker compose -f deploy/docker-compose.yml build

# ② 导出镜像（NAS 拿不到你的构建缓存，直接传 tar 最省事）
docker save shipin-fabu:1.8.0 -o shipin-fabu-1.8.0.tar

# ③ 拷到 NAS（scp / 共享目录 / U 盘都行）
scp shipin-fabu-1.8.0.tar admin@<NAS地址>:/volume1/docker/shipin-fabu/

# ── 在 NAS 上 ──
cd /volume1/docker/shipin-fabu

# ④ 更新前先备份数据（一条命令，几秒钟）
docker compose -f deploy/docker-compose.yml stop
tar czf data-backup-$(date +%Y%m%d-%H%M).tar.gz data/

# ⑤ 载入新镜像并切换
docker load -i shipin-fabu-1.8.0.tar
#    改 docker-compose.yml 里 image: shipin-fabu:1.8.0
docker compose -f deploy/docker-compose.yml up -d

# ⑥ 确认跑的是新版（也能在界面顶栏看到版本号）
curl -s http://127.0.0.1:8947/api/health
```

### 回滚

```bash
# 把 docker-compose.yml 的 image 改回旧标签，再 up -d 即可
docker compose -f deploy/docker-compose.yml up -d
```
**为什么回滚是安全的**：数据库迁移**只增不减**（`ALTER TABLE ADD COLUMN`，只加列）→ 旧版本读新库不会崩。

### 更新的三条纪律

1. **别在发布/抓取进行中更新** —— 重启会中断任务。虽然有断点恢复（重启后把 `running` 标记为失败可重试），但那是给意外崩溃兜底的，不该当常规手段。
2. **不要用 watchtower 之类自动更新** —— 它会自作主张重启，正好撞上发布中。
3. **依赖变了必须重建镜像**，不能只换几个 `.py` 文件 —— playwright/patchright/浏览器二进制都烤在镜像里。

---

## 5. 备份

要备份的**只有 `data/` 一个目录**（代码在镜像里）：

```bash
tar czf data-backup-$(date +%Y%m%d).tar.gz data/
```

里面包含：素材视频、封面、SQLite 数据库、**各平台登录态 profile**、配置（含口令哈希）、AI 密钥。
> 登录态丢了要重新扫码；数据库丢了发布记录全没。**建议每周备一次，更新前必备。**

---

## 6. 反代（可选，要域名/HTTPS 时用）

```bash
docker compose -f deploy/docker-compose.yml --profile proxy up -d
```
Nginx 只做转发和 TLS 卸载，**鉴权在应用里**，别再叠一层 basic auth。

用域名访问时**必须**设环境变量，否则 Host 白名单会拦掉：

```yaml
environment:
  VP_ALLOWED_HOSTS: "fabu.example.com"
```

---

## 7. 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| 容器起不来，日志说「拒绝启动：…没有设置访问口令」 | 见第 2 节，先设口令 |
| 日志 `Target page, context or browser has been closed` | profile 被占（另一个浏览器实例在用）。本项目启动时会自动清理遗留窗口；仍不行就 `docker compose restart` |
| 页面能开但接口全 401 | 没登录或会话过期（7 天）。重新输口令 |
| 403 `Host 不在允许列表` | 用域名访问但没设 `VP_ALLOWED_HOSTS` |
| 抓取一直是「未接入真实抓取」 | 该平台的真实抓取还没实现（目前只有抖音），这是如实标注，不是故障 |
| 浏览器起不来 / 崩溃 | 给容器加 `shm_size: 1gb`（compose 里已有）；再不行看 `docker exec shipin-fabu cat /tmp/xvfb.log` |
| 扫码后一直不跳转 | 大概率就是 **Xvfb 没骗过风控** → 走第 3 节的保底方案 |

---

## 8. 已知约束（别当故障）

- **发布需要人工**：抖音发布弹短信验证码，这是平台风控，不是本工具的缺陷。
- **头条号/西瓜视频的选择器未校准**：头条号需要账号后跑一次 `app/tools/recon_platform.py toutiao`；西瓜视频与抖音共用后台（`inherits: douyin`），已实测可用。
- **真实抓取目前只有抖音**：`app/data_worker.py` 里的 `SUPPORTED` 是唯一事实来源，其余平台在界面上如实显示「未接入真实抓取」——**不会再拿模拟数据充数**。

# 短视频发布台 · Windows 桌面包

完整解压到固定目录，双击 `Install.bat` 选择数据目录并创建桌面快捷方式，再双击 `Start.bat`。
无需安装 Git 或 Python。自动启动默认关闭，安装时可选择登录 Windows 后启动。
启动窗口关闭后，服务和它启动的浏览器进程会一起结束。

视频、数据库、登录态和日志保存在所选数据目录，默认 `%LOCALAPPDATA%/shipin-fabu/data`。
程序目录必须可写以支持更新；数据目录不可与 `app`、`runtime`、`browsers`、`launcher` 重叠。
排查启动失败时查看数据目录里的 `logs/server.log`。

账号页点击登录会打开本机浏览器；登录完成后先保存登录态，再自动关闭所有登录标签页并刷新状态。
登录态保存在所选数据目录的 `browsers/`，包含各平台独立 profile 和私有 `*.storage.json` 备份。
工具重启、发布和采集均复用这些数据；session cookie 也会恢复，更新程序不会覆盖登录态。
关闭已完成的登录窗口不会退出账号；取消重新登录会保留原状态。平台使会话过期或失效后需重新登录。
NAS 部署仍保留网页二维码和 noVNC 登录入口。
素材应通过素材库上传：只复制视频文件不会生成数据库中的素材记录。
不要把共享 NAS 的其他同事登录态复制到自己的电脑。

## 程序与数据结构

```text
Start.bat / Install.bat
launcher/               固定启动器、更新、安装、进程管理
runtime/python/         Python 3.12 x64 与锁定依赖
browsers/               两个浏览器驱动对应的 Chromium 版本
app/                    可独立更新的业务程序
install.json            本机安装配置（更新不覆盖）
所选数据目录/           app.db、视频、登录态、logs、update.status.json
```

更新先验证必需的 size、MD5 和可选 SHA256，再解压到固定 `app.new`。
事务日志记录替换阶段；未完成的事务在下次启动时离线恢复旧版。
新版健康检查必须返回本次进程的身份及数据目录身份，通过后才确认更新。
启动失败会关闭新版进程树、恢复旧版、重启一次，并跳过该失败版本直到发布后续版本。
同一程序包和同一数据目录分别持有系统文件锁，重复双击只打开现有实例。
不兼容的依赖锁或启动器协议要求重新下载完整绿色包。

## 维护与打包

源码运行：在项目根执行 `python -X utf8 launcher/start.py --dev --no-updates`。
正式包只使用自带运行环境，不回落到系统 Python。

准备一套 Python 3.12 x64 环境，安装 `app/requirements-desktop.lock` 中的全部精确版本，
并用该环境执行 `python -m playwright install chromium`。
两个驱动如要求不同版本，另执行 `python -m patchright install chromium`。
在项目根执行：

```powershell
python -X utf8 launcher/assemble.py _scratch/shipin-fabu-1.10.1 --browsers "$env:LOCALAPPDATA/ms-playwright" --archive
python -X utf8 launcher/package.py app _scratch/releases-1.10.1
```

组装使用生产文件白名单，并用包内 Python 和浏览器运行自检；任何缺失都会失败。
输出目录必须不存在，失败时保留产物用于排查；修复后使用新的输出目录重新打包。
版本号只修改 `app/server.py` 的 `APP_VERSION`，`app/VERSION` 自动生成并回读验证。
同版本不能生成不同内容的安装包。依赖或启动器变化必须发完整绿色包。

Gitee 分发仓库：https://gitee.com/guizhou-jubangbang_0/short-video-posting-platform-dist
将 ZIP 上传到对应版本的 Releases 附件，再把生成的 `version.json` 放到仓库 `dist/version.json`。
默认清单源为上述仓库的 `raw/main/dist`，清单中的 `url` 指向 Releases 附件。
ZIP 不进入 Git；公开分发仓库只放使用说明、更新清单和发布附件，不能包含本机配置、账号数据或无关文件。
目前未执行公开发布。

可在 `install.json` 的 `update_sources` 配置 NAS 和 Gitee 等多个源；下载或校验失败会换源。
`update_sources: []` 可关闭远程更新。在线发布源仍需真实下载验收。

★ 更新源**默认必须是 HTTPS**（2026-10-06 安全审查 R7）：明文 HTTP 源上能同时替换清单和包的人
可以让替换后的包通过摘要校验（摘要来自同一个清单，不构成独立的发布者身份）。
内网 NAS 若只能用 HTTP，需要显式设置环境变量 `VP_UPDATE_ALLOW_HTTP=1`，且目标必须是
环回或 RFC1918 私网地址（`127.x` / `10.x` / `172.16-31.x` / `192.168.x`）——
公网 HTTP 地址设置了这个开关也会被拒。清单和安装包都校验**重定向之后的最终地址**，
HTTPS 入口被重定向到 HTTP 同样会被拒。

## 验收

依次运行，不能并行：

```powershell
cd app
python -X utf8 tests/test_28_launcher.py
python -X utf8 tests/test_29_desktop.py
python -X utf8 tests/test_30_login_autoclose.py
```

开发机验收不能替代同事电脑验收：公开发布前还需在未安装 Python/Git 的 Windows 电脑上
验证完整解压、安装快捷方式、本机登录、素材上传、模拟发布和更新；真实平台操作由账号持有人完成。

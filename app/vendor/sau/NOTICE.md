# vendored：social-auto-upload（第三方代码说明）

本目录下的 `uploader/`、`utils/`、`myUtils/` 来自开源项目
**social-auto-upload**（https://github.com/dreammis/social-auto-upload），
许可证 **MIT**（原文见同目录 `LICENSE`，**请勿删除**）。

- 取用时间：2026-09-27
- 取用方式：GitHub tarball（main 分支快照，非 git clone）
- 集成方式：由本项目 `app/sau_bridge.py` 调用，本项目**不修改其业务逻辑**

---

## 只取了本项目需要的部分

| 取了 | 没取 |
|---|---|
| `uploader/`：douyin / tencent(视频号) / ks(快手) / xiaohongshu / weibo / bilibili + `base_video.py` | 它自带的 Web 后台（`sau_backend/`、`sau_frontend/`、`db/`）——本项目有自己的界面 |
| `utils/`：base_social_media、files_times、constant、browser_hook、network、stealth.min.js | tk(TikTok) / youtube / alipay / hupu / baijiahao / xhs 等本项目不用的上传器 |
| `myUtils/` | 它依赖的 Flask / SQLAlchemy / alembic 等一整条 Web 栈 |

## 本项目做的改动（共 12 处，均可对照上游 diff）

| 文件 | 改动 | 为什么 |
|---|---|---|
| `conf.py` | **新增**（上游只提供 `conf.example.py`，需用户自行复制） | 上游的 `conf.py` 不随仓库分发；这里给出集成所需取值，并支持用环境变量 `SAU_BASE_DIR` 把 cookies/验证码文件等**运行期产物**指到数据目录，保持源码树干净 |
| `utils/login_qrcode.py` | **替换为本项目实现** | 上游此文件依赖 **cv2 + numpy + segno** 三个重依赖，只为「解二维码图片 / 终端画画」。本项目**不使用它的扫码登录**（登录仍走 `app/login_worker.py` 的持久化 profile），故用轻量替身顶掉：保留上传器 import 期会引用的函数名，真正需要 QR 解码的函数显式报错而不是静默返回错值 |
| `utils/log.py` | **替换为本项目实现** | 上游依赖 loguru。替身用标准库实现同名 logger，并额外把日志**追加到 `SAU_LOG_FILE`** —— 发布进程靠读它把「平台要求短信验证码」这类提示转达给 UI |
| `utils/base_social_media.py` | `set_init_script()` 里 stealth.min.js 的定位方式：`BASE_DIR/utils/stealth.min.js` → `Path(__file__).parent/stealth.min.js` | 上游写法把 `BASE_DIR` 钉死在代码目录，导致运行期产物只能落在源码树里。改后 `BASE_DIR` 可指向数据目录 |
| `uploader/tencent_uploader/main.py` | `_build_launch_kwargs()`：`launch_kwargs["channel"] = "chrome"` → `"chromium"` | 上游这一处写的是 `"chrome"`（**要求系统装 Google Chrome**），而本项目的镜像里只有 playwright/patchright 自带的 chromium。其余上传器本来就用 `"chromium"`，**只有视频号不一样**，于是抖音发得出去、视频号一提交就炸：`Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome`。回归测试：`app/tests/test_14_sau_browser.py` |

| `uploader/tencent_uploader/main.py` | `format_str_for_short_title()`：补足词 `"，精彩内容分享"` → `"精彩内容分享"`，且补完**再过滤一遍** | 上游补足词开头是**全角逗号**，而本函数自己的过滤器恰恰不允许全角逗号（它只把半角 `,` 换成空格，其余非字母数字一律丢）；补足又发生在过滤**之后**，等于绕过自己的检查。标题不足 7 字时必然踩中：`「测试」→「测试，精彩内容」`，视频号的「发表」按钮会**一直是灰的、点不动**，发布重试 5 分钟最终失败（实测 2026-09-28，job_12）。回归测试：`app/tests/test_21_topics_short_title.py` |

| `uploader/ks_uploader/main.py` | `cookie_auth()`：正向证明「上传按钮可见」的等待 `timeout=10000` → `45000` | 上游整个判断窗口只有 13 秒（3 秒固定等待 + 10 秒等按钮）。快手创作页是前端渲染，稍慢就赶不上，于是落进兜底分支「**无法确认就按失效处理**」，整条发布被拦，用户看到「cookie 已失效」而账号明明好好的。实测 2026-09-28：job_21 就这样失败，而同一 profile 隔几分钟用同一套逻辑再跑，上传按钮稳稳可见。与本项目「坑 10」（抖音登录卡 t+20s 才渲染）同类 —— **宁可等，不要误判** |

| `uploader/weibo_uploader/main.py` | `_open_video_publish_page()`：`filter(has_text="视频")` → `filter(has_text=re.compile(r"^视频$"))` | 上游用**子串**匹配取「首页第一个含『视频』的 span」，而微博首页现在有两个：`[16]「视频音乐」`（左侧栏导航项）排在 `[33]「视频」`（真正的发布入口）**前面**，`.first` 拿到的是前者 → 点了不弹发布窗口 → `Timeout 30000ms exceeded while waiting for event "popup"`（实测 2026-09-28 job #27）。改成整串精确匹配后 count=1，点下去稳稳开出 `weibo.com/upload/channel` |

| `uploader/weibo_uploader/main.py` | `_select_declaration()`：触发下拉的选择器由哈希类名（`_gap1_nsgmr` / `_tit1_nsgmr`）改为语义类 `div.wbpro-form span.woo-pop-ctrl`，老两条降为兜底 | 上游两条选择器**都带 CSS-module 哈希**，平台一发版哈希就变 → **两条一起失效** → `Timeout 15000ms exceeded`，发布卡死在「内容声明」这步。实测 2026-09-28：两条均 0 命中，而 `div.wbpro-form span.woo-pop-ctrl` 命中 1 个、文本正是「内容声明」（两个类名都不含哈希） |

| `uploader/weibo_uploader/main.py` | `_select_declaration()`：找不到「含AI生成内容」选项时**先把现场落盘再抛**（新增 `_dump_declaration_failure()`，写 `BASE_DIR/weibo_decl_failure.json` + `.png`） | 这一步的 DOM **只有视频传完之后才出现** —— 人工和探针都够不到（表单没显示时，触发元素在 DOM 里但不可点）。而它一旦失败就是静默超时，没法标定。这正合本项目决策 3.4「**失败必须给证据，不能只是说明**」：落一份精简摘要（可见的候选 + `.woo-pop-ctrl` 全量）+ 截图，够标定用，不塞盘 |

| `uploader/weibo_uploader/main.py` | `_select_declaration()`：选项文案**写死**「含AI生成内容」→ 改为候选表 `DECL_OPTION_TEXTS = ("内容由AI生成", "含AI生成内容")` 按序试（新文案在前、旧的兜底；首条等 8 秒，兜底各 2 秒） | 微博把 AI 那条的**文案改掉了**。实测 2026-09-29（job_32）：弹层里 5 个选项是「无 / 内容为自主创作 / 内容为转载 / **内容由AI生成** / 内容为虚构演绎」，**根本没有「含AI生成内容」** → 8 秒超时、发布卡死「内容声明」这一步。**这正是第 10 处那段落盘代码抓到的**，一次标定成功（`data/sau/weibo_decl_failure.png` 截图里看得一清二楚，JSON 里也抓到了那个 BUTTON 的完整祖先路径）。同一次落盘还暴露：`_panel_nsgmr` / `_footer_nsgmr` / `_checkActive_nsgmr` 这些哈希类**全部消失**（弹层改成 `woo-pop-main.woo-pop-down`，且**没有「确定」footer**），所以面板 scope 会退回整页、选中态校验会恒报"确认不了"——已把那句日志改成**照实说"无法确认"**，不再断言「未激活」。回归测试：`app/tests/test_24_weibo_declaration.py` |

| `uploader/weibo_uploader/main.py` | `_upload_thumbnail()` 末尾调一次本项目的弹窗收尾 `dialog_settle.settle_async`（带 try/except 兜底，单独跑上游时退化为原行为） | 封面之后微博**还会弹一次确认**（用户 2026-09-29 实测）。老代码在「封面已上传并完成」就返回了，残留层会把后面的 合集/描述/发布 全挡住 —— 表现为"封面好了，后面点不动"。收尾模块见 `10-弹窗收尾-设计.md`；回归测试 `app/tests/test_25_dialog_settle.py` 判据 25~28 |

> 除上表 12 处外，`uploader/` 下各平台代码**逐字节保持上游原样**，便于将来对照升级。

## 集成要点（写给将来的维护者）

- **登录态怎么接**：本项目用持久化 Edge profile，上游用 `storage_state` JSON。
  `app/sau_bridge.py` 在发布前用本项目的 playwright 打开 profile 并导出 JSON，当作
  `account_file` 交给上游上传器 —— 所以**本项目的扫码登录流程与账号页无需改动**。
- **短信验证码**：上游会在 `BASE_DIR/verify_code.txt` 找验证码（读走即删）。
  实测抖音会弹短信验证码，**必须有人在弹出的浏览器窗口里输入** —— 所以发布要走**有头模式**。
- **浏览器**：上游**绝大多数**上传器用 `channel="chromium"`，即 **patchright 自带的 chromium**，
  与本项目主流程用的 msedge 是两套。首次使用需 `python -m patchright install chromium`。
  > ⚠️ **例外是视频号**：上游那一处原本写 `channel="chrome"`（要求系统装 Google Chrome），
  > 已按上表第 5 处改掉。**升级上游覆盖同名文件时，这一处会被覆盖回去**，
  > 覆盖后务必跑 `app/tests/test_14_sau_browser.py` —— 不跑就会重现「抖音能发、视频号炸」。
- **升级上游**：重新拉取同名文件覆盖即可，注意保留上表 **10** 处改动。
  覆盖 `tencent_uploader/main.py` 后，除了浏览器那处，短标题补足词那处也会被覆盖回去 ——
  跑 `app/tests/test_14_sau_browser.py` 与 `app/tests/test_21_topics_short_title.py` 验一下。

# GLaDOS 自动签到（真实浏览器点击版）

用 GitHub Actions 每天北京时间 **09:00 唤醒**，脚本内再**随机等待 0–90 分钟**，
然后用真实 Chromium 打开签到页、**模拟点击签到按钮**完成签到。

## 为什么改成浏览器点击

2026 年 9 月改版后，GLaDOS 只认新版会话，并且**用浏览器算出的设备指纹校验"登录设备"**：

| 现象 | 原因 |
| --- | --- |
| `{"code":-2,"message":"没有权限"}` | 旧版 `koa:sess` 会话、或 Cookie 不完整 |
| `code=4 / reason=device-mismatch`<br>`Automated check-in detected` | **设备指纹对不上**（见下） |
| 用 requests 直连总失败 | 缺少真实浏览器的 TLS 指纹、`Sec-CH-UA`、站点自身请求头 |

### 设备指纹是什么（关键）

前端 `app.bundle.js` 里有一段指纹代码，把所有 API 请求都带上：

```js
xt.interceptors.request.use(e => (Cl && (e.headers.Authorization = Cl), e));
// Cl = localStorage["glados.fp.v1"] || "fp1." + SHA256(7 段特征)[:48]
```

7 段特征依次是：`v1`、canvas 渲染哈希、WebGL 厂商/渲染器哈希、
`屏幕宽x高x色深xDPR`、`时区/1月与7月时区偏移`、
`language|languages|platform|硬件线程|deviceMemory|maxTouchPoints|cookieEnabled`、
localStorage 可用性。算出来缓存在 localStorage，键名 `glados.fp.v1`。

你的浏览器里存的是"登录那台机器"的指纹，服务端认；而 Actions 的 runner 是
Linux、无 GPU、无中文字体，实时算出来的 canvas/WebGL/platform 全都不一样，
于是稳定返回 `device-mismatch`。**所以必须把登录设备的指纹导出来喂给脚本。**

本仓库旧版直接用 `requests` POST `/api/user/checkin`，实测与「不带 Cookie 请求」
返回完全相同的 `没有权限`。现在的方案改成：

1. 用 Playwright 启动真实 Chromium（`--headless=new`）；
2. 注入完整 Cookie + 登录设备的指纹，打开 `https://glados.cloud/console/checkin`；
3. 定位站点自己的签到按钮 `button.checkin-cute-btn` 并**真的点击**；
4. 抓取站点发出的 `/api/user/checkin` 真实响应判定结果，再读取会员天数与积分。

## 前置条件（必须手动做一次）

在**登录 GLaDOS 的那台**电脑上：

1. 拉取本仓库并准备隔离环境：`pwsh -File setup_env.ps1`；
2. 打开 <https://glados.cloud> 退出后**重新登录**；
3. F12 → Network → 刷新 → 点开一次成功的 `/api/user/status`；
4. 在 **Request Headers → Cookie** 里右键复制**完整的一行值**，确认同时包含
   `gld:sess=` 与 `gld:sess.sig=`；
5. 导出设备指纹（会弹出一个浏览器窗口，请保持它开着几秒）：

   ~~~powershell
   & .venv\Scripts\python.exe print_fingerprint.py
   ~~~
6. 访问 `chrome://version`，复制**完整的「用户代理 / User-Agent」**；
7. 到仓库 **Settings → Secrets and variables → Actions**：

| 名称 | 类型 | 内容 |
| --- | --- | --- |
| `GLADOS_COOKIE` | Secret | 第 4 步复制的完整 Cookie |
| `GLADOS_FP` | Secret | 第 5 步输出的 `fp1.…` |
| `GLADOS_USER_AGENT` | Variable | 第 6 步复制的完整 User-Agent |

三项都来自同一个浏览器才会一致：**Cookie + 指纹 + UA 必须同源**。
以后换电脑、换浏览器或清理过浏览器数据（localStorage 被清空），都要重新走一遍 4–6 步。

### 支持的 Cookie 格式

完整单行请求头（推荐，原样透传，不做 HTML/URL 解码）：

~~~text
koa:sess=旧会话; koa:sess.sig=旧签名; gld:sess=新会话; gld:sess.sig=新签名
~~~

也支持：带 `Cookie:` 前缀、整段外层引号、Cookie-Editor 的 JSON 数组。

Cookie **不需要**四项都齐：只要同时有 `gld:sess` 和 `gld:sess.sig` 就算合格，
`koa` 两项可有可无、有多余字段也不影响（都原样透传）。真实长度参考：
`gld:sess` 约 52 字符、`gld:sess.sig` 约 29 字符，两项合起来整串也就 100 字节出头，
所以脚本**不会**因为"整串短"而拒绝。会被拒绝并给出提示的只有三种：

| 情况 | 提示 |
| --- | --- |
| 只有 `koa:sess` + `koa:sess.sig` | 旧版会话，需重新登录取 `gld` 会话 |
| 缺 `gld:sess` 或 `gld:sess.sig` | 列出实际拿到的字段名 |
| 字段名齐但值只有几个字符 | `…值只有 N 个字符，不像完整会话值…基本可以确定复制被截断` |

最后一种要求在 Network 里右键 **Cookie → Copy value** 整行粘贴（手动框选长串容易截断）。

## 随机签到时间

- 工作流定时：`cron: "0 1 * * *"` = 北京时间 **09:00 唤醒**；
- 脚本内等待 `GLADOS_JITTER_MIN_MINUTES`–`GLADOS_JITTER_MAX_MINUTES`
  （默认 `0`–`90`）之间的**随机**时长，然后才点击签到；
- 也就是实际签到落在 **09:00–10:30** 之间的随机时刻，每天不同；
- **手动 `Run workflow` 不等待**（识别 `workflow_dispatch` 后立即签到），
  方便你随时验证；只有定时任务才做随机等待；
- 本地也可以 `--no-jitter` 跳过等待；
- 这段等待确实占用 Actions 的运行时长（私有仓库按分钟计费，按墙钟时间算，
  90 分钟等待就是 90 分钟额度）。本仓库是公开仓库，Actions 分钟数免费且不限量，
  所以没有额外开销；如果之后改成私有仓库，可把 `GLADOS_JITTER_MAX_MINUTES`
  调小（例如 30）来省额度；
- 日志会打印随机等待时长和真实点击时间，方便核对；
- GitHub 定时任务在高峰期本身可能延迟，实际时间会相应后移（日志以真实点击时间为准）。

## 配置项

在 **Settings → Secrets and variables → Actions**：

| 名称 | 类型 | 默认与用途 |
| --- | --- | --- |
| `GLADOS_COOKIE` | Secret | 必填；完整 Cookie |
| `GLADOS_FP` | Secret | 必填；登录设备的 `fp1.…` 指纹 |
| `GLADOS_USER_AGENT` | Variable | 建议填；登录浏览器的完整 UA |
| `GLADOS_JITTER_MIN_MINUTES` | Variable | 0；随机等待下限（分钟） |
| `GLADOS_JITTER_MAX_MINUTES` | Variable | 90；随机等待上限（分钟，最大 120） |

本地还可以用 `GLADOS_FP_FILE` 指向存放指纹的文件（省去复制粘贴），
以及 `GLADOS_BROWSER_CHANNEL=chrome|msedge` 复用系统浏览器。

失败邮件沿用旧配置（可留空）：`SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` /
`SMTP_PASSWORD` / `SMTP_USE_SSL` / `MAIL_TO` / `MAIL_FROM`。
邮件正文只包含脱敏后的错误说明与 Actions 链接，不含 Cookie。

## 排查工具

| 脚本 | 用途 |
| --- | --- |
| `diag_status.py` | 只发一次 `/api/user/status`，判断 Cookie 现在还有没有效（不起浏览器） |
| `print_fingerprint.py` | 在登录设备上导出 `fp1.…` 指纹 |
| `checkin.py --dry-run` | 打开签到页、报告登录态与按钮状态，但不点击、不等待 |

常见对照：

| 日志 | 结论 |
| --- | --- |
| `code=-2 没有权限` | Cookie 无效/不完整（先用 `diag_status.py` 确认） |
| `code=4 reason=device-mismatch` | 指纹或 UA 与登录设备不一致，重新导出 `GLADOS_FP` |
| `页面 error：net::ERR_CONNECTION_TIMED_OUT` | runner 到站点网络抖动，脚本会自动重试 |
| `发起签到请求：POST …` 没出现 | 点击没触发接口，看重试后的现场日志与截图 |

## 结果与退出码

- `code=0`，或 `Checkin!`、`Checkin Repeats! Please Try Tomorrow`、
  `Today's observation logged` → 成功（含今日已签到）；
- 按钮处于 `disabled`/`is-done`（站点自身"已完成"状态）→ 按今日已签到处理；
- `没有权限` / `HTTP 401/403` → 致命：Cookie 需要重新获取；
- `code=4` 且 `reason=device-mismatch` → 致命：指纹/UA 需要与登录设备对齐；
- 超时、429、5xx → 同一次运行内最多重试 2 次（间隔 60 秒）。

退出码：`0` 成功，`1` 配置/认证类错误，`2` 临时故障。
失败时会把截图上传为 `checkin-failure-screenshots` 工件（保留 7 天）。

## 本机运行（依赖装在仓库内的隔离虚拟环境）

不会往系统默认 Python 里装任何包：

~~~powershell
pwsh -File setup_env.ps1          # 创建 .venv，安装 requests + playwright，并尽量下载 Chromium

# 导出登录设备的指纹（会弹出浏览器窗口）
& .venv\Scripts\python.exe print_fingerprint.py

# 确认 Cookie 当前是否有效（只查一次 status，不起浏览器）
& .venv\Scripts\python.exe -u diag_status.py

# 只看登录态，不等待随机时间、不点击（推荐先跑这个）
$env:GLADOS_COOKIE='gld:sess=...; gld:sess.sig=...'
$env:GLADOS_FP='fp1.xxxxxxxx...'
& .venv\Scripts\python.exe checkin.py --dry-run --channel chrome

# 真正签到（会随机等待，用 --no-jitter 可跳过等待）
& .venv\Scripts\python.exe checkin.py --no-jitter --channel chrome

# 离线单测（不需要浏览器）
& .venv\Scripts\python.exe -B -m unittest -q
~~~

`--channel chrome`（或 `msedge`）让 Playwright 直接用系统里已装好的浏览器，
不必下载 Chromium；CI 上则用 `python -m playwright install --with-deps chromium`。
浏览器缓存默认写到 `.ms-playwright/`（已在 `.gitignore`），不污染用户目录。

`.venv/`、`artifacts/`、`__pycache__/` 同样已加入 `.gitignore`。

## 定时任务与保活

工作流支持手动 `Run workflow` 测试；`concurrency` 保证不会同时跑两个签到。
保活工作流保持原样：每 45 天提交一次空提交，避免定时任务被停用，
需要仓库 Settings → Actions → General → Workflow permissions 开启 Read and write。

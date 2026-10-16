# GLaDOS 自动签到（真实浏览器点击版）

用 GitHub Actions 每天北京时间 **09:00 唤醒**，脚本内再**随机等待 0–90 分钟**，
然后用真实 Chromium 打开签到页、**模拟点击签到按钮**完成签到。

## 为什么改成浏览器点击

2026 年 9 月改版后，GLaDOS 只认新版会话，并会校验登录设备：

| 现象 | 原因 |
| --- | --- |
| `{"code":-2,"message":"没有权限"}` | 旧版 `koa:sess` 会话、或 Cookie 不完整 |
| `code=4 / reason=device-mismatch` | 签到浏览器与登录设备不一致 |
| 用 requests 直连总失败 | 缺少真实浏览器的 TLS 指纹、`Sec-CH-UA`、站点自身请求头 |

本仓库旧版直接用 `requests` POST `/api/user/checkin`，实测与「不带 Cookie 请求」
返回完全相同的 `没有权限`，说明服务端根本没认出该会话。现在的方案改成：

1. 用 Playwright 启动真实 Chromium（`--headless=new`）；
2. 注入完整 Cookie，打开 `https://glados.cloud/console/checkin`；
3. 定位站点自己的签到按钮 `button.checkin-cute-btn` 并**真的点击**；
4. 抓取站点发出的 `/api/user/checkin` 真实响应判定结果，再读取会员天数与积分。

## 前置条件（必须手动做一次）

在**同一台**电脑的 Chrome/Edge 上：

1. 打开 <https://glados.cloud> 退出后**重新登录**；
2. F12 → Network → 刷新 → 点开一次成功的 `/api/user/status`；
3. 在 **Request Headers → Cookie** 里复制**完整的一行值**，确认同时包含
   `gld:sess=` 与 `gld:sess.sig=`（旧的只有 `koa:sess` 两项是不够的）；
4. 访问 `chrome://version`，复制**完整的「用户代理 / User-Agent」**；
5. 到仓库 **Settings → Secrets and variables → Actions**：

| 名称 | 类型 | 内容 |
| --- | --- | --- |
| `GLADOS_COOKIE` | Secret | 第 3 步复制的完整 Cookie |
| `GLADOS_USER_AGENT` | Variable | 第 4 步复制的完整 User-Agent |

第 5 步的 UA 不是可选项：设备校验是按登录设备算的，UA 与 Cookie 必须来自
**同一次登录的同一个浏览器**，否则会稳定拿到 `device-mismatch`。

### 支持的 Cookie 格式

完整单行请求头（推荐，原样透传，不做 HTML/URL 解码）：

~~~text
koa:sess=旧会话; koa:sess.sig=旧签名; gld:sess=新会话; gld:sess.sig=新签名
~~~

也支持：带 `Cookie:` 前缀、整段外层引号、Cookie-Editor 的 JSON 数组、
以及只有两个裸值的旧格式。脚本会拒绝明显过短（< 300 字节）、字段名齐但值被截断、
缺 `gld:sess.sig` 或只有 `koa` 会话的输入，并直接告诉你该重新取哪一项，
不再向服务端发注定失败的请求。如果日志里出现
`Cookie 字段名正确（gld:sess + gld:sess.sig），但整串只有 XXX 字节`，
说明复制被截断了，请在 Network 里右键 **Cookie → Copy value** 整行粘贴。

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

在 **Settings → Secrets and variables → Actions → Variables**：

| Variable | 默认与用途 |
| --- | --- |
| `GLADOS_USER_AGENT` | **建议填**登录浏览器的完整 UA，用于通过设备校验 |
| `GLADOS_JITTER_MIN_MINUTES` | 0；随机等待下限（分钟） |
| `GLADOS_JITTER_MAX_MINUTES` | 90；随机等待上限（分钟，最大 120） |

失败邮件沿用旧配置（可留空）：`SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` /
`SMTP_PASSWORD` / `SMTP_USE_SSL` / `MAIL_TO` / `MAIL_FROM`。
邮件正文只包含脱敏后的错误说明与 Actions 链接，不含 Cookie。

## 结果与退出码

- `code=0`，或 `Checkin!`、`Checkin Repeats! Please Try Tomorrow`、
  `Today's observation logged` → 成功（含今日已签到）；
- `没有权限` / `HTTP 401/403` → 致命：Cookie 需要重新获取；
- `code=4` 且 `reason=device-mismatch` → 致命：需在同一台电脑重新登录并同步更新
  Cookie 与 `GLADOS_USER_AGENT`；
- 超时、429、5xx、被识别为自动化 → 同一次运行内最多重试 2 次（间隔 60 秒）。

退出码：`0` 成功，`1` 配置/认证类错误，`2` 临时故障。
失败时会把截图上传为 `checkin-failure-screenshots` 工件（保留 7 天）。

## 本机运行（依赖装在仓库内的隔离虚拟环境）

不会往系统默认 Python 里装任何包：

~~~powershell
pwsh -File setup_env.ps1          # 创建 .venv，安装 requests + playwright，并尽量下载 Chromium

# 只看登录态，不等待随机时间、不点击（推荐先跑这个）
$env:GLADOS_COOKIE='gld:sess=...; gld:sess.sig=...'
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

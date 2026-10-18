# GLaDOS 自动签到（Cookie 接口 / 浏览器）

默认复用已有 Cookie，通过 GitHub Actions 在北京时间 **09:00–10:30** 的随机时刻启动并立即签到。
每次结束后生成下一次时间，保存到 `config/schedule.json` 并同步工作流 cron，不让 runner 随机空等。
随机排程需要配置下文的 `SCHEDULE_TOKEN`；它与 GLaDOS 登录凭据相互独立。

## 推荐：复用 Cookie 调用签到接口

参考 [pyx13638516490/glados_checkin 的接口流程](https://github.com/pyx13638516490/glados_checkin/blob/main/checkin.py)，
本项目提供 Cookie API 模式，向 `https://glados.cloud/api/user/checkin` 发送
`{"token":"glados.cloud"}`，验证响应后查询会员天数与积分。此模式不重新登录、不申请邮箱验证码，
也不安装或启动 Chromium；临时网络故障最多重试一次，业务拒绝不会盲目重发。
仅请求 `glados.cloud`，不自动把 Cookie 转发到其他域名。

登录风控与已有会话签到是不同步骤：邮箱发码接口返回 `captcha_required`，
只说明本次申请验证码需要人机验证；有效 Cookie 可以复用已有登录态。
这不保证 Cookie 永不过期，也不保证签到接口永远不要求验证；服务端明确拒绝时会停止。

仓库 **Settings → Secrets and variables → Actions** 中设置：

| 名称 | 类型 | 值 |
| --- | --- | --- |
| `GLADOS_AUTH_MODE` | Variable | `cookie`；从邮箱模式切换时必须修改原来的 `email` |
| `GLADOS_CHECKIN_METHOD` | Variable，可选 | `api`（默认）；`browser` 则复用 Cookie 用浏览器点击 |
| `GLADOS_COOKIE` | Secret | 已登录浏览器中复制的完整 Cookie 请求头 |
| `GLADOS_CHECKIN_TOKEN` | Variable，可选 | 默认 `glados.cloud`；旧值 `glados.one` 会自动纠正 |
| `GLADOS_FP` | Secret，可选 | 仅在需要设备校验时填写同一已登录浏览器的真实缓存指纹 |
| `GLADOS_USER_AGENT` | Variable，可选 | 同一登录浏览器的完整 User-Agent |

Cookie 获取步骤：

1. 用你平时的浏览器打开 [GLaDOS](https://glados.cloud)，手动完成登录及站点要求的人机验证。
2. F12 → **Network**，刷新控制台，点开一次成功的 `/api/user/status` 或 `/api/user/session` 请求。
3. 在 **Request Headers → Cookie** 中复制完整值（不要复制 `Set-Cookie`），保存为 `GLADOS_COOKIE`。
4. 保留该请求中实际出现的所有字段。完整 `gld:sess` / `gld:sess.sig` 或完整 `koa:sess` / `koa:sess.sig`
   都允许提交，不能只根据字段名称判断是否过期；是否有效由服务端回复决定。
5. 同步最新代码后手动 **Run workflow**，确认日志出现 `Cookie 接口模式` 和明确的签到成功回复。

Cookie 支持完整单行请求头、带 `Cookie:` 前缀、外层引号或单域名 Cookie-Editor JSON。
签名值不做 HTML/URL 解码，完整请求头不重排、不丢字段；缺少完整会话或明显复制截断会报错。
Cookie 是登录凭据，只保存到 Secret，不提交到仓库，也不要贴到聊天中。
Cookie 失效后需要在浏览器重新登录、更新 Secret；脚本不会静默切回邮箱模式反复申请验证码。

若接口明确返回 **`device-mismatch`**，在**刚才导出 Cookie 的已登录网页**控制台读取：

```javascript
localStorage.getItem('glados.fp.v1') // 完整结果存为 Secret GLADOS_FP
navigator.userAgent                // 完整结果存为 Variable GLADOS_USER_AGENT
```

三个值来自同一登录浏览器。`print_fingerprint.py` 会新建独立浏览器，不能导出你原浏览器
已有的登录缓存，因此其探测值不能与原浏览器 Cookie 混用。
`没有权限` 本身不能证明 TLS/浏览器指纹有问题，更不能据此认定所有 koa Cookie 已失效。
旧版 `diag_status.py` 曾漏发 Cookie，会造成“没有权限”的误判；现已补齐请求头并加入回归测试。

## 可选：QQ 邮箱验证码登录

需要邮箱模式时将 `GLADOS_AUTH_MODE` 设为 `email`。运行时通过浏览器申请验证码、
用 QQ IMAP 读取、在同一浏览器完成登录与签到；这个模式每次都会重新登录。
站点要求人机验证时脚本会停止，需要人工处理，不会自动解决验证码挑战。

在 [QQ 邮箱](https://mail.qq.com/) 设置中开启 IMAP/SMTP 服务并生成客户端授权码，
按 [QQ 官方说明](https://help.mail.qq.com/detail/106/985) 操作。配置：

| 名称 | 类型 | 内容 |
| --- | --- | --- |
| `GLADOS_EMAIL` | Secret | GLaDOS 登录邮箱，即验证码收件地址 |
| `QQ_IMAP_AUTH_CODE` | Secret | QQ 邮箱客户端授权码，不是 QQ 密码或临时验证码 |
| `GLADOS_MAIL_FROM` | Secret | 验证码邮件 From 中的完整发件地址，不是你自己的收件邮箱 |
| `QQ_IMAP_USERNAME` | Secret，可选 | QQ IMAP 登录邮箱，默认使用 `GLADOS_EMAIL` |
| `GLADOS_CODE_REGEX` | Variable，可选 | 默认 `\b[0-9]{6}\b`，六位纯数字无需另填 |
| `GLADOS_MAIL_TIMEOUT_SECONDS` | Variable，可选 | 默认 180 秒，允许 15–600 秒 |

邮件读取仅限 INBOX、指定发件人和 To 收件人，排除申请前已有的邮件与过早到达的邮件。
使用只读 IMAP 和 `BODY.PEEK[]`，不标记已读、不删除邮件。转发改变 To、进入垃圾箱或提取
出多个不同验证码时会失败；正则最多一个捕获组，可按真实邮件文字调整。
邮箱模式忽略外部 Cookie、FP 与 UA。`--dry-run` 会拒绝邮箱模式，防止只读检查意外发码。

登录页先等待主文档响应，再等待邮箱输入框；仅发码前的页面访问会重试。
登录响应体可能因立即跳转而丢失，脚本会复核本站控制台跳转与 `/api/user/session`，
服务端返回 `HTTP 200 / code=0` 才继续。明确的 JSON 拒绝和人机验证不会进入成功兜底。
日志只记录阶段、HTTP/内容类型、固定资源分类和报错数量，不输出验证码或登录页截图。

## 凭据与故障判断

| 现象 | 处理 |
| --- | --- |
| 发码接口 `captcha_required` | 本次邮箱登录需要人工验证；可复用手动登录后的有效 Cookie |
| Cookie API `没有权限` / `code=-2` | 本次会话未被接受，检查是否复制完整、是否已过期或被吊销 |
| 签到接口 `device-mismatch` | 核对同一登录浏览器的 Cookie、缓存指纹和 UA |
| `please checkin via ...` | 核对签到 token，当前默认 `glados.cloud` |
| `未配置 SCHEDULE_TOKEN` | 随机排程未启用，需要独立配置 GitHub Token；不代表邮箱或 Cookie 错误 |
| `未配置完整 SMTP` | 未启用失败邮件通知，与 QQ IMAP 或签到认证是否成功无关 |

本地 Python 需要可用的 SSL/TLS；CI 使用 Python 3.12。Cookie API 只依赖 `requirements.txt`；
邮箱模式和浏览器点击备选还需要 `requirements-browser.txt` 与 Chromium 或已安装的 Chrome/Edge。

## 随机签到时间

每次手动或定时运行都立即签到。无论本次签到成功或失败，任务都会尝试为**明天**选择一个
随机分钟偏移，原子提交 `config/schedule.json` 和 `.github/workflows/checkin.yml`，
并在日志与运行摘要中展示下一次时间。正常运行之间不会占用 runner 等待。

GitHub 的 `on.schedule` 使用静态 cron，不能在触发前动态读取仓库文件，
所以脚本会把 config 的时间同步成 UTC cron；cron 同时指定下一次的日/月，
避免改成今天较晚的时间后当天再次触发。首次部署保留北京时间 09:00 的每日启动入口，
第一次运行完成后才切换为指定日期的随机触发。
参见 [GitHub 调度说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。

随机范围在 `config/schedule.json` 中修改：

```json
{
  "base_time": "09:00",
  "min_offset_minutes": 0,
  "max_offset_minutes": 90,
  "next_offset_minutes": null,
  "next_run_at": null,
  "cron": "0 1 * * *"
}
```

上面是首次部署的初始状态。你只需要修改前三项：基准时间按北京时间填写，
偏移需满足 `0 <= min <= max <= 120`，且窗口不能跨过午夜。
后三项由脚本维护；例如随机到 `37`，会保存明天 `09:37:00+08:00`，同时生成对应 cron。
修改范围后可手动运行一次签到，使新范围应用到下一次排程。
原来的 `GLADOS_JITTER_MIN_MINUTES`、`GLADOS_JITTER_MAX_MINUTES` 已不再使用，
`--no-jitter` 仍兼容旧命令，但现在不加这个参数也不会随机等待。
网络/接口故障后的短暂重试仍保留。

### 一次性配置 SCHEDULE_TOKEN

自动改写工作流需要额外的工作流写权限，默认 `GITHUB_TOKEN` 不足以完成该操作。
创建仅限这个仓库的 fine-grained personal access token：

1. GitHub 右上角头像 → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**。
2. **Resource owner** 选择仓库所属账号；**Repository access** 选 **Only select repositories**，只勾选 `glados_checkin`。
3. **Repository permissions** 设置 **Contents: Read and write**、**Workflows: Read and write**；Metadata 保持自动的 Read。
4. 生成 Token，把它保存到签到仓库 **Settings → Secrets and variables → Actions → Secrets → New repository secret**，名称为 **`SCHEDULE_TOKEN`**，值仅填写 Token 本身。
5. 同步最新代码到默认分支，手动 **Run workflow** 一次，确认 **Save the next random checkin time** 步骤成功，仓库 config 与 cron 已自动更新。

Token 的有效期按你的账号策略设置，到期需更新 Secret；它不替代邮箱授权码。
权限依据：[GitHub Token 创建说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)、
[工作流文件写权限](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens)。
若默认分支保护阻止该 Token 直接提交，排程更新会失败；脚本不会强推或绕过分支保护。

缺少 Token 或更新失败时，本次签到仍会执行，但排程步骤报错，远程原 cron 不变。
原有保活工作流每天检查一次：若已安排的日期早于今天，会尝试补排到今天尚可用的窗口，
窗口已过则安排明天；待执行的今天排程不会因 GitHub 延迟而被提前改写。
保活修复同样需要有效 Token；更新失败需要根据日志修正权限/有效期。
GitHub 的调度仍可能延迟或遗漏，设置的分钟不能保证准点到达。

每日自动提交只修改 config 和 cron 标记块，并保留其他文件；遇到并发提交会重新读取默认分支，
不使用强制更新。你在本地修改仓库前先拉取远程更新，避免和自动排程提交冲突。
本地可用 `python random_schedule.py --preview` 预览下一次时间，该命令不访问 GitHub，也不保存改动。

## 配置项

在 **Settings → Secrets and variables → Actions**：

| 名称 | 类型 | 默认与用途 |
| --- | --- | --- |
| `GLADOS_COOKIE` | Secret | 必填；完整 Cookie |
| `GLADOS_FP` | Secret | 可选；明确出现设备不匹配时，从同一登录网页读取的真实缓存值 |
| `GLADOS_USER_AGENT` | Variable | 可选；同一登录浏览器的完整 UA |
| `SCHEDULE_TOKEN` | Secret | 自动保存下次随机触发时间；仅本仓库 Contents/Workflows 读写权限 |

本地还可以用 `GLADOS_FP_FILE` 指向存放指纹的文件（省去复制粘贴），
以及 `GLADOS_BROWSER_CHANNEL=chrome|msedge` 复用系统浏览器。

失败邮件沿用旧配置（可留空）：`SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` /
`SMTP_PASSWORD` / `SMTP_USE_SSL` / `MAIL_TO` / `MAIL_FROM`。
邮件正文只包含脱敏后的错误说明与 Actions 链接，不含 Cookie。

## 排查工具

| 脚本 | 用途 |
| --- | --- |
| `diag_status.py` | 只发一次 `/api/user/status`，判断 Cookie 现在还有没有效（不起浏览器） |
| `print_fingerprint.py` | 独立测试浏览器的指纹探测；不能替代原登录网页的缓存导出 |
| `checkin.py --dry-run` | Cookie API 模式只查询 status，不提交签到；浏览器模式检查页面和按钮 |

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
- `没有权限` / `HTTP 401/403` → 致命：检查 Cookie 是否被接受或站点是否要求验证；
- `code=4` 且 `reason=device-mismatch` → 致命：指纹/UA 需要与登录设备对齐；
- 超时、429、5xx → Cookie API 最多请求两次（间隔 5 秒）；浏览器模式按原有 60 秒间隔重试。

退出码：`0` 成功，`1` 配置/认证类错误，`2` 临时故障。
浏览器签到失败时可上传 `checkin-failure-screenshots`（保留 7 天）；Cookie API 模式不生成截图。

## 本机运行（依赖装在仓库内的隔离虚拟环境）

不会往系统默认 Python 里装任何包：

~~~powershell
pwsh -File setup_env.ps1          # 创建 .venv，安装 requests + playwright，并尽量下载 Chromium

# 从已登录网页 Network 复制 Cookie 后，检查会话（不签到、不起浏览器）
$env:GLADOS_AUTH_MODE='cookie'
$env:GLADOS_CHECKIN_METHOD='api'
$env:GLADOS_COOKIE='gld:sess=...; gld:sess.sig=...'
& .venv\Scripts\python.exe checkin.py --dry-run

# 真正签到（立即执行；--no-jitter 仅为兼容旧命令）
& .venv\Scripts\python.exe checkin.py --no-jitter

# 离线单测（不需要浏览器）
& .venv\Scripts\python.exe -B -m unittest -q
~~~

需要浏览器点击备选时，设 `GLADOS_CHECKIN_METHOD=browser`。
`--channel chrome`（或 `msedge`）让 Playwright 直接用系统里已装好的浏览器，
不必下载 Chromium；CI 上则用 `python -m playwright install --with-deps chromium`。
浏览器缓存默认写到 `.ms-playwright/`（已在 `.gitignore`），不污染用户目录。

`.venv/`、`artifacts/`、`__pycache__/` 同样已加入 `.gitignore`。

## 定时任务与保活

工作流支持手动 `Run workflow` 测试；`concurrency` 保证不会同时跑两个签到。
保活工作流每天北京时间 08:17 唤醒，仍保留每 45 天提交一次空提交的行为，
并检查随机排程是否过期；过期时使用 `SCHEDULE_TOKEN` 修复。
空提交保活需要仓库 Settings → Actions → General → Workflow permissions 开启 Read and write。

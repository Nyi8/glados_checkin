# GLaDOS 自动签到（真实浏览器点击版）

用 GitHub Actions 在北京时间 **09:00–10:30** 之间的随机时刻启动，
启动后立即用真实 Chromium 打开签到页并点击签到，**不再让 runner 随机空等**。
每次运行结束后生成下一次的随机时间，保存到 `config/schedule.json`，并同步更新工作流 cron。

## 推荐方案：QQ 邮箱验证码自动登录

设置 `GLADOS_AUTH_MODE=email` 后，每次任务在 **Actions 自己启动的同一个浏览器**里：

1. 通过 GLaDOS 登录页的 `Get Code` 申请验证码；
2. 通过 QQ IMAP TLS 读取本次申请后收到的验证码邮件；
3. 在登录页输入验证码并点击 `Login`；
4. 直接用这个浏览器完成签到，结束后销毁浏览器会话。

Cookie 和站点生成的设备指纹都来自本次运行，避免把电脑上的 Cookie 搬到另一台设备。
邮箱模式忽略 `GLADOS_COOKIE`、`GLADOS_FP`、`GLADOS_USER_AGENT`、`GLADOS_FP_FILE`；
原 Cookie 模式仍是默认值，不更改配置就保持原有行为。

**一次性配置**：

先在 [QQ 邮箱](https://mail.qq.com/) 设置中开启 IMAP/SMTP 服务，生成客户端授权码。
这里使用的是**邮箱授权码**，不是 QQ 密码，也不是 GLaDOS 发来的临时验证码。
可参阅 [QQ 官方授权码帮助](https://service.mail.qq.com/detail/0/1087)。
在一封真实的 GLaDOS 验证码邮件中展开发件人，记下完整的发件地址（不要只复制显示名），
并确认验证码格式。然后在仓库 **Settings → Secrets and variables → Actions** 配置：

| 名称 | 类型 | 内容 |
| --- | --- | --- |
| `GLADOS_AUTH_MODE` | Variable | `email`，启用新模式 |
| `GLADOS_EMAIL` | Secret | 你的 GLaDOS 登录邮箱地址 |
| `QQ_IMAP_AUTH_CODE` | Secret | QQ 邮箱生成的客户端授权码 |
| `GLADOS_MAIL_FROM` | Secret | 验证码邮件的完整发件地址，精确匹配 |
| `QQ_IMAP_USERNAME` | Secret，可选 | QQ IMAP 登录邮箱；默认使用 `GLADOS_EMAIL`，别名登录时可单独指定 |
| `GLADOS_CODE_REGEX` | Variable，可选 | 默认 `\b[0-9]{6}\b`；格式不同时按实际邮件修改 |
| `GLADOS_MAIL_TIMEOUT_SECONDS` | Variable，可选 | 默认 180 秒，允许 15–600 秒 |

若验证码是六位字母数字混合，可用带上下文的正则，例如
`Access Code:\s*([A-Za-z0-9]{6})`，需要与邮件的实际文字一致。
正则最多一个捕获组；存在多个不同的匹配结果时不提交，避免把其他数字当成验证码。

配置后手动执行一次 **Run workflow** 验证完整登录与签到；配置下文的 `SCHEDULE_TOKEN` 后，
即可每天在随机时刻启动并立即签到。脚本无需新增第三方依赖。
已有失败邮件配置可以继续使用；它和读取验证码的 IMAP 配置相互独立。
授权码只存入 Secrets，不要写进代码、提交到 Git 或粘贴到聊天里。

脚本以只读方式打开 INBOX，使用 `BODY.PEEK[]` 获取邮件，不标记已读、不删除邮件。
发送验证码前记录已有邮件 UID，并检查新邮件的服务器接收时间、发件人及 To 收件人；
只接受发给 `GLADOS_EMAIL` 的邮件，因此转发导致 To 不匹配、垃圾箱收信或超时都会失败。
邮箱授权码、验证码和 Cookie 不写入日志，也不上传登录页截图或浏览器会话。

**自动化的实际边界**：当前前端支持 `captcha_required` 人机验证分支。
遇到它、账号迁移、IMAP 授权码失效或站点继续拒绝自动签到时，脚本会停止并沿用失败通知，
不会反复发送验证码。自动读邮箱能解决续登录与跨设备搬 Cookie 的问题，不能保证绕过所有
站点风控。频繁要求人机验证时，更适合迁移到固定设备/服务器并保留浏览器登录状态；
本次实现的 Actions 邮箱模式每次都会重新登录，未实现跨运行保存登录状态。

本地测试使用设置好环境变量的终端执行 `python checkin.py --no-jitter --channel chrome`。
邮箱模式下 `--dry-run` 会直接拒绝运行，防止只读检查意外发送验证码。
本地 Python 必须具备正常的 SSL/TLS 支持；CI 使用 Python 3.12。

邮箱模式排查：

| 日志 | 含义与处理 |
| --- | --- |
| `QQ IMAP 已登录并以只读方式打开收件箱` | 本次邮箱授权码已通过验证，不需要因为后续网页超时重新生成授权码 |
| `stage: 打开登录页`、`net::ERR_CONNECTION_TIMED_OUT` | 运行机器到 GLaDOS 的页面访问失败；脚本在发码前最多尝试两次 |
| `stage: 等待邮箱输入框` | 主文档已经响应，但登录表单未就绪；检查诊断中的 `app-script`、`session-api` 状态和 `script_errors` |
| `stage: 申请验证码` | 点击 Get Code 或等待发码接口失败；为避免重复发送验证码，不自动重试 |
| `stage: 提交登录` / `等待登录跳转` | 已进入提交验证码阶段；查看登录接口或跳转是否完成 |
| `响应体无法读取，可能已发生页面跳转` | 浏览器跳转后原响应体可能不可用；脚本将复核控制台跳转与服务端会话，不重复提交验证码 |
| `登录态复核：HTTP 200，code=0` | 服务端已确认当前浏览器会话有效，随后才继续签到 |
| `未配置完整 SMTP，跳过失败邮件` | 只表示未启用失败邮件通知，与 QQ IMAP 收验证码是否可用无关 |

登录页导航只等待主文档响应（`commit`），随后单独等待邮箱输入框可见，
避免其他延迟加载的资源拖住 `DOMContentLoaded`。诊断只记录阶段、固定资源分类、
HTTP 状态、浏览器网络错误码和脚本报错数量；不记录登录页正文、查询串、验证码或截图。
当前运行仍因访问限制或网络连接失败而停止时，需要根据这些诊断确认运行环境；
不能把一次超时直接认定为邮箱授权码错误。

认证接口会记录 HTTP 状态和经过筛选的 Content-Type，先识别 403、429 和 5xx，
再解析 JSON。提交验证码后，如果响应体不可读取或不是有效 JSON，只有在跳转到本站
控制台且使用当前 Cookie 与站点生成的设备指纹查询 `/api/user/session` 返回成功时，
才认定登录成功。明确的 JSON 拒绝和人机验证回复不会进入这个兜底分支。
工作流另有隔离浏览器回归测试，拦截全部请求验证跳转与会话校验，不使用任何账号凭据。

以下章节描述原有 **Cookie 模式** 的配置与排查。

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
| `GLADOS_FP` | Secret | 必填；登录设备的 `fp1.…` 指纹 |
| `GLADOS_USER_AGENT` | Variable | 建议填；登录浏览器的完整 UA |
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

# 真正签到（立即执行；--no-jitter 仅为兼容旧命令）
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
保活工作流每天北京时间 08:17 唤醒，仍保留每 45 天提交一次空提交的行为，
并检查随机排程是否过期；过期时使用 `SCHEDULE_TOKEN` 修复。
空提交保活需要仓库 Settings → Actions → General → Workflow permissions 开启 Read and write。

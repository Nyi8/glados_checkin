# GLaDOS 自动签到

这个仓库提供一个基于 Python `requests` 的 `glados.cloud` 自动签到脚本，并通过 GitHub Actions 每天定时执行。

## 技术方案

脚本按固定两步执行：

1. 调用签到接口 `POST https://glados.cloud/api/user/checkin`
2. 调用状态接口 `GET https://glados.cloud/api/user/status`，读取 `data.leftDays`

如果签到请求异常、返回 HTTP 错误或返回未知结果，脚本会等待 10 分钟后再次签到。只有第二次仍然失败时，才会发送失败邮件并让 GitHub Actions 任务失败。

鉴权方式直接使用完整的 Cookie 字符串，从环境变量 `GLADOS_COOKIE` 读取，并原样放入请求头 `Cookie`。这适合包含 `koa:sess` 和 `koa:sess.sig` 的场景，不需要在脚本里单独拆分。

默认签到请求体为：

```json
{
  "token": "glados.one"
}
```

如果后续站点调整了 token 或域名，可以通过环境变量覆盖：

- `GLADOS_BASE_URL`
- `GLADOS_CHECKIN_TOKEN`

## 目录结构

```text
.
|-- .github
|   `-- workflows
|       |-- checkin.yml
|       `-- keepalive.yml
|-- checkin.py
|-- requirements.txt
`-- README.md
```

## 本地运行

1. 安装依赖

```bash
pip install -r requirements.txt
```

2. 设置环境变量

```bash
export GLADOS_COOKIE='koa:sess=xxx; koa:sess.sig=yyy'
```

Windows PowerShell:

```powershell
$env:GLADOS_COOKIE='koa:sess=xxx; koa:sess.sig=yyy'
```

3. 执行脚本

```bash
python checkin.py
```

## GitHub Actions 配置

工作流文件为 `.github/workflows/checkin.yml`。

- 触发时间：`0 1 * * *`
- 含义：每天 `01:00 UTC`
- 换算为北京时间：每天早上 `09:00`（UTC+8）

同时保留了 `workflow_dispatch`，可以在 GitHub 页面手动点一次运行，方便测试。

仓库保活工作流为 `.github/workflows/keepalive.yml`。它每天检查最近一次提交的时间（`00:17 UTC`，北京时间 `08:17`），只有距最近一次提交达到 45 天时才会创建一个空提交，从而保持定时 Actions 活跃。

## GitHub 上的配置步骤

1. 在 GitHub 新建仓库，并把本目录文件推送上去。
2. 进入仓库页面的 `Settings`。
3. 打开 `Secrets and variables` -> `Actions`。
4. 点击 `New repository secret`。
5. 名称填写 `GLADOS_COOKIE`。
6. 值填写浏览器里复制出来的完整 Cookie 字符串，至少要包含：

```text
koa:sess=...; koa:sess.sig=...
```

7. 如果要接收失败邮件，继续添加下面的 SMTP Secrets。
8. 保存后进入 `Actions` 页面。
9. 首次可以手动执行 `GLaDOS Checkin` 工作流，确认日志正常。

## 失败邮件配置

在 `Settings` -> `Secrets and variables` -> `Actions` 中添加以下 Repository secrets：

| 名称 | 说明 |
| --- | --- |
| `SMTP_HOST` | SMTP 服务器，例如 `smtp.gmail.com` |
| `SMTP_PORT` | SMTP 端口，587 或 465；不填时默认为 587 |
| `SMTP_USERNAME` | SMTP 登录用户名，通常是邮箱地址 |
| `SMTP_PASSWORD` | SMTP 密码或邮箱应用专用密码 |
| `MAIL_TO` | 接收失败提醒的邮箱地址 |
| `MAIL_FROM` | 发件人地址，可选；不填时使用 `SMTP_USERNAME` |
| `SMTP_USE_SSL` | 可选；使用 465 端口时填写 `true`，587 端口填写 `false` |

例如 Gmail 通常使用 `smtp.gmail.com`、端口 `587`、`SMTP_USE_SSL=false`，并使用应用专用密码。脚本不会把 Cookie 放入邮件内容。

邮件配置缺失时，签到失败仍会正常让任务失败，但日志会提示邮件未发送以及缺少哪些配置。

## 保活权限

`keepalive.yml` 需要向仓库推送空提交。进入 `Settings` -> `Actions` -> `General` -> `Workflow permissions`，选择 `Read and write permissions` 并保存。工作流文件本身也声明了 `contents: write` 权限。

## 日志输出

脚本会在控制台打印：

- 签到接口 URL
- 签到接口 HTTP 状态码
- 签到接口返回 JSON
- 状态接口 HTTP 状态码
- 状态接口返回 JSON
- 当前剩余天数
- 第一次失败后的 10 分钟重试结果
- 连续失败时的邮件发送结果

## Cookie 获取说明

在浏览器登录 `https://glados.cloud` 后：

1. 打开开发者工具
2. 进入 `Application` 或 `Storage`
3. 找到站点 Cookie
4. 复制完整 Cookie 字符串
5. 确保其中包含 `koa:sess` 和 `koa:sess.sig`

建议不要只复制单个字段，直接复制整段 Cookie 并写入 `GLADOS_COOKIE`。

## 后来者补充（主要是值的规范）

### 1、新版GLADOS_COOKIE 

如何获取   F12找到本地存储，里面有的，参考如下  **没有引号**  嫌麻烦可以用Cookie-Editor Edge扩展

Repository secrets

```txt
Cookie: gld:sess=content;gld:sess.sig=content
```
### 2、新版GLADOS_USER_AGENT 

获取同一台cookie获取网页，F12 控制台navigator.userAgent，复制值，参考如下 **没有引号**
Repository variables

```
  Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0
```

### 3、一次性配置 SCHEDULE_TOKEN

自动改写工作流需要额外的工作流写权限，默认 `GITHUB_TOKEN` 不足以完成该操作。 创建仅限这个仓库的 fine-grained personal access token：

1. GitHub 右上角头像 → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**。

2. **Resource owner** 选择仓库所属账号；**Repository access** 选 **Only select repositories**，只勾选 `glados_checkin`。

3. **Repository permissions** 设置 **Contents: Read and write**、**Workflows: Read and write**；Metadata 保持自动的 Read。

4. 生成 Token，把它保存到签到仓库 **Settings → Secrets and variables → Actions → Secrets → New repository secret**，名称为 **`SCHEDULE_TOKEN`**，值仅填写 Token 本身。（参考如下）

```txt
   github_pat_11ABCDEFG0a1b2c3d4eXXXXXXXXXXXXXXXXXXXXXXXXX
```
5. 同步最新代码到默认分支，手动 **Run workflow** 一次，确认 **Save the next random checkin time** 步骤成功，仓库 config 与 cron 已自动更新。

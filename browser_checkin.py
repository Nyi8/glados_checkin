"""用真实 Chromium 打开 GLaDOS 签到页，模拟点击签到按钮。

为什么不直接 POST /api/user/checkin：
  GLaDOS 2026 版接口会校验登录设备，并且只有真实浏览器才会带上与登录设备
  一致的 Sec-CH-UA、Accept-Language、TLS 指纹和站点自身注入的请求头。
  用 requests 直连时，旧版 koa 会话或非浏览器指纹会直接得到
  {"code":-2,"message":"没有权限"}。

支持导入 Cookie 或通过 QQ 邮箱验证码登录，退出码语义与 checkin.py 一致：
  0 成功或今日已签到，1 需要人工处理的致命错误，2 临时故障。
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime

from email_login import QQMailbox, email_mode, login as login_with_email
from glados_cookie import (
    BEIJING_TZ,
    FatalError,
    RetryableError,
    get_env,
    log,
    parse_cookie_pairs,
    redact,
    require_cookie,
    session_cookie_kind,
)

CHECKIN_URL = "https://glados.cloud/console/checkin"
LOGIN_URL = "https://glados.cloud/login"
CHECKIN_API = re.compile(r"/api/user/checkin(?:$|\?)")
CHECKIN_BUTTON = "button.checkin-cute-btn"
ARTIFACT_DIR = "artifacts"

# GLaDOS 前端把设备指纹缓存在 localStorage，并作为 Authorization 请求头发出；
# 服务端据此校验"登录设备"，不一致就返回 code=4 / device-mismatch。
FP_STORAGE_KEY = "glados.fp.v1"
FP_PATTERN = re.compile(r"^fp1\.[0-9a-f]{16,64}$")

EXIT_OK, EXIT_FATAL, EXIT_RETRYABLE = 0, 1, 2

NAVIGATION_TIMEOUT_MS = 60_000
SELECTOR_TIMEOUT_MS = 45_000
CLICK_TIMEOUT_MS = 45_000
RETRY_DELAY_SECONDS = 60
MAX_ATTEMPTS = 2
MAX_JITTER_MINUTES = 120

SUCCESS_MARKERS = ("checkin! got", "checkin repeats", "observation logged")
ALREADY_CHECKED_IN_MARKERS = (
    "checkin repeats", "observation logged", "already checkin", "already checked",
    "try tomorrow", "已签到", "今天已签到", "今日已签到", "明天再来", "重复签到",
    "签到了", "下次再来",
)
AUTH_ERROR_MARKERS = (
    "没有权限", "未登录", "请先登录", "登录已过期", "not login",
    "not logged", "please login", "unauthorized", "invalid cookie",
    "cookie expired", "session expired",
)
DEVICE_MISMATCH_MARKER = "device-mismatch"
AUTH_ERROR_PAGE_MARKERS = AUTH_ERROR_MARKERS + (
    "no permission", "permission denied", "forbidden", "login expired",
)
AUTOMATION_MARKERS = ("automated check-in", "automated checkin", "automation detected")


class CheckinResult:
    """一次签到尝试的结果，同时保存原始接口回复以便排错。"""

    def __init__(self, status, message="", payload=None, source="unknown"):
        self.status = status  # success | already | fatal | retryable
        self.message = message
        self.payload = payload or {}
        self.source = source

    @property
    def ok(self):
        return self.status in ("success", "already")


def classify_payload(payload):
    """把 /api/user/checkin 的 JSON 回复映射成 CheckinResult。"""
    if not isinstance(payload, dict):
        return CheckinResult("retryable", "签到接口返回的不是 JSON 对象", source="api")
    code = payload.get("code")
    message = str(payload.get("message", "")).strip()
    lowered = message.lower()
    reason = str(payload.get("reason", "")).strip().lower()

    if reason == DEVICE_MISMATCH_MARKER or (code == 4 and "mismatch" in reason):
        return CheckinResult("fatal", message or "登录设备不匹配", payload, "api")

    if any(marker in message for marker in AUTH_ERROR_MARKERS) or code == -2:
        return CheckinResult("fatal", message or "会话未被接受", payload, "api")

    if code == 0:
        if any(marker in lowered for marker in ALREADY_CHECKED_IN_MARKERS):
            return CheckinResult("already", message, payload, "api")
        return CheckinResult("success", message, payload, "api")

    if any(marker in lowered for marker in SUCCESS_MARKERS):
        return CheckinResult("success", message, payload, "api")
    if any(marker in lowered for marker in ALREADY_CHECKED_IN_MARKERS):
        return CheckinResult("already", message, payload, "api")

    if any(marker in lowered for marker in AUTOMATION_MARKERS):
        return CheckinResult("retryable", message or "被识别为自动化请求", payload, "api")

    return CheckinResult("retryable", message or f"未知回复 code={code}", payload, "api")


def classify_page_state(text, payload=None):
    """点击后拿不到接口回复时，用页面渲染出的文案判断结果。"""
    if payload:
        return classify_payload(payload)
    lowered = (text or "").lower()
    if any(marker in lowered for marker in ALREADY_CHECKED_IN_MARKERS):
        return CheckinResult("already", text.strip()[:200], source="dom")
    if any(marker in lowered for marker in SUCCESS_MARKERS):
        return CheckinResult("success", text.strip()[:200], source="dom")
    if any(marker in lowered for marker in AUTH_ERROR_PAGE_MARKERS):
        return CheckinResult("fatal", text.strip()[:200], source="dom")
    if any(marker in lowered for marker in AUTOMATION_MARKERS):
        return CheckinResult("retryable", text.strip()[:200], source="dom")
    return CheckinResult("retryable", "没有捕获到签到接口回复，页面也没有结果文案", source="dom")


def jitter_seconds(min_minutes, max_minutes):
    """返回本次运行要随机等待的秒数；区间非法时抛 FatalError。"""
    if not 0 <= min_minutes <= max_minutes <= MAX_JITTER_MINUTES:
        raise FatalError(
            f"随机等待区间非法：需要 0 <= 最小 <= 最大 <= {MAX_JITTER_MINUTES} 分钟，"
            f"当前为 {min_minutes}–{max_minutes}。"
        )
    if max_minutes == min_minutes:
        return float(min_minutes * 60)
    return random.SystemRandom().uniform(min_minutes * 60, max_minutes * 60)


def sleep_with_jitter(min_minutes, max_minutes):
    seconds = jitter_seconds(min_minutes, max_minutes)
    if seconds < 1:
        log("随机等待：0 秒（本次不等待）。")
        return 0
    log(f"随机等待 {seconds / 60:.1f} 分钟（区间 {min_minutes}–{max_minutes} 分钟）后再签到，"
        f"避免每天同一时间签到。")
    time.sleep(seconds)
    return seconds


def _playwright_version():
    try:
        from importlib.metadata import version

        return version("playwright")
    except Exception:  # pragma: no cover - 仅用于日志
        return "unknown"


def _cookie_list(cookie_header):
    # Playwright 要求 url 与 path 二选一，不能同时给。
    cookies = []
    for name, value in parse_cookie_pairs(cookie_header):
        cookies.append({
            "name": name,
            "value": value,
            "url": "https://glados.cloud",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        })
    return cookies


def _user_agent():
    return get_env("GLADOS_USER_AGENT") or None


def get_fingerprint():
    """读取浏览器设备指纹 fp1.<hex>，来源：GLADOS_FP 或 GLADOS_FP_FILE。"""
    value = get_env("GLADOS_FP")
    path = get_env("GLADOS_FP_FILE")
    if not value and path:
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError as exc:
            raise FatalError(f"读取 GLADOS_FP_FILE 失败：{exc}") from exc
    if not value:
        return ""
    if not FP_PATTERN.match(value):
        raise FatalError(
            "GLADOS_FP 格式不正确：应当是 fp1. 开头加 16-64 位十六进制，"
            f"当前长度 {len(value)}；请用 python -u print_fingerprint.py 重新导出。"
        )
    return value


def _install_fingerprint(context, fingerprint):
    """把指纹写进 localStorage，让站点的指纹计算函数直接命中缓存。

    站点逻辑是：localStorage["glados.fp.v1"] 命中正则就拿来当 Authorization；
    否则用 canvas/webgl/时区等实时计算。runner 上没有 GPU、platform 也是 Linux，
    实时算出来的必然与登录设备不一致，所以这里必须沿用登录时的缓存值。
    """
    context.add_init_script(
        """
        (() => {
          const KEY = %s;
          const VALUE = %s;
          try {
            const original = Storage.prototype.getItem;
            Storage.prototype.getItem = function (key) {
              if (key === KEY) return VALUE;
              return original.call(this, key);
            };
            if (window.localStorage) window.localStorage.setItem(KEY, VALUE);
          } catch (error) { /* localStorage 不可用时忽略 */ }
        })();
        """
        % (json.dumps(FP_STORAGE_KEY), json.dumps(fingerprint))
    )


def _screenshot(page, tag):
    if get_env("GLADOS_SCREENSHOT_ON_FAIL", "1") == "0":
        return None
    try:
        os.makedirs(ARTIFACT_DIR, exist_ok=True)
        stamp = datetime.now(BEIJING_TZ).strftime("%Y%m%d-%H%M%S")
        path = os.path.join(ARTIFACT_DIR, f"{tag}-{stamp}.png")
        page.screenshot(path=path, full_page=True)
        log(f"已保存失败截图：{path}")
        return path
    except Exception as exc:  # 截图失败不能影响签到结论
        log(f"WARNING: 截图失败：{redact(exc)}")
        return None


def _button_is_done(page):
    """站点用 .checkin-cute-btn.is-done 表示今日已签到（按钮同时 disabled）。"""
    try:
        classes = page.locator(CHECKIN_BUTTON).first.get_attribute("class", timeout=5_000) or ""
        return "is-done" in classes.split()
    except Exception:
        return False


def _button_state(page):
    """返回 (是否存在, 是否禁用, 按钮文案)。"""
    locator = page.locator(CHECKIN_BUTTON).first
    try:
        if locator.count() == 0:
            return False, False, ""
        text = (locator.inner_text(timeout=5_000) or "").strip()
        disabled = locator.is_disabled(timeout=5_000)
        return True, disabled, text
    except Exception as exc:
        log(f"WARNING: 读取签到按钮状态失败：{redact(exc)}")
        return False, False, ""


def _page_text(page, limit=2000):
    try:
        return (page.inner_text("body", timeout=5_000) or "")[:limit]
    except Exception:
        return ""


def read_account(page):
    """用浏览器会话读会员天数与积分；展示失败不影响签到结论。"""
    for path, field in (("/api/user/status", "leftDays"), ("/api/user/points", "points")):
        try:
            response = page.request.get(
                f"https://glados.cloud{path}",
                headers={"Accept": "application/json, text/plain, */*"},
                timeout=NAVIGATION_TIMEOUT_MS,
            )
            if not response.ok:
                log(f"WARNING: {path} 查询失败 HTTP {response.status}；签到结果保持成功。")
                continue
            data = response.json()
            if not isinstance(data, dict):
                log(f"WARNING: {path} 返回非对象；签到结果保持成功。")
                continue
            if data.get("code") not in (0, None):
                log(f"WARNING: {path} code={data.get('code')}；签到结果保持成功。")
                continue
            container = data.get("data") if isinstance(data.get("data"), dict) else data
            value = container.get(field) if isinstance(container, dict) else None
            log(f"{field}={value if value is not None else '<unavailable>'}")
        except Exception as exc:
            log(f"WARNING: {path} 查询失败：{redact(exc)}；签到结果保持成功。")


SESSION_LOST_HINT = (
    "会话未生效：签到页把请求重定向走了（未登录/会话失效时 GLaDOS 会回到首页或登录页）。"
    "请在 glados.cloud 用电脑浏览器重新登录，从成功的 /api/user/status 请求里复制完整 Cookie"
    "（同时含 gld:sess 与 gld:sess.sig）更新 GLADOS_COOKIE，并把同一浏览器的完整 "
    "User-Agent 写到 GLADOS_USER_AGENT。"
)


def _attempt(page, cookie_header=None):
    """打开签到页并点击一次，返回 CheckinResult。"""
    log(f"打开签到页：{CHECKIN_URL}")
    try:
        page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    except Exception as exc:
        raise RetryableError(f"打开签到页失败：{redact(exc)}") from exc

    log(f"页面标题={page.title()!r}，当前地址={page.url}")
    if "/login" in page.url:
        raise FatalError(SESSION_LOST_HINT)
    if "/console" not in page.url:
        raise FatalError(f"{SESSION_LOST_HINT}（当前被重定向到 {page.url}）")

    # 等 React 渲染出签到按钮；首页/登录页不会有这个按钮。
    try:
        page.wait_for_selector(CHECKIN_BUTTON, timeout=SELECTOR_TIMEOUT_MS)
    except Exception as exc:
        text = _page_text(page)
        raise RetryableError(
            f"签到按钮没有出现（页面可能改版、没加载完或会话被弹回）：{redact(exc)}；"
            f"当前地址={page.url}；页面文案片段={text[:200]!r}"
        ) from exc

    exists, disabled, label = _button_state(page)
    if not exists:
        raise RetryableError("没有找到签到按钮，站点结构可能已变化。")
    done_class = _button_is_done(page)
    log(f"签到按钮：文案={label!r}，disabled={disabled}，is-done={done_class}")

    if disabled:
        text = _page_text(page)
        result = classify_page_state(text)
        if result.ok:
            log(f"按钮不可点击，页面显示今日已完成：{result.message}")
            return result
        # 站点自己的完成态就是最强证据：按钮被禁用即今日已签到，不要误判为失败。
        log(f"按钮不可点击（is-done={done_class}），按“今日已签到”处理；页面文案片段={text[:200]!r}")
        return CheckinResult("already", label or "按钮已禁用（今日已签到）", source="dom")

    try:
        log("点击签到按钮…")
        # 诊断用：把点击瞬间的按钮状态和随后所有 /api/user/checkin 请求记录下来。
        try:
            log(f"点击前按钮：disabled={page.locator(CHECKIN_BUTTON).first.is_disabled(timeout=5_000)}，"
                f"文案={(page.locator(CHECKIN_BUTTON).first.inner_text(timeout=5_000) or '').strip()!r}")
        except Exception as exc:
            log(f"WARNING: 读取点击前按钮状态失败：{redact(exc)}")
        with page.expect_response(
            lambda response: bool(CHECKIN_API.search(response.url))
            and response.request.method.upper() == "POST",
            timeout=CLICK_TIMEOUT_MS,
        ) as response_info:
            page.locator(CHECKIN_BUTTON).first.click(timeout=CLICK_TIMEOUT_MS)
        response = response_info.value
        log(f"签到接口 HTTP {response.status}：{response.url}")
        try:
            payload = response.json()
        except Exception as exc:
            raise RetryableError(f"签到接口返回非 JSON 内容（HTTP {response.status}）。") from exc
        result = classify_payload(payload)
        log(f"签到接口 code={payload.get('code')}，message={payload.get('message', '<none>')}，"
            f"reason={payload.get('reason', '<none>')}，points={payload.get('points', '<none>')}")
        return result
    except (FatalError, RetryableError):
        raise
    except Exception as exc:
        # 点击超时或接口异常：先看页面自己渲染出的结果，避免误报失败。
        log(f"WARNING: 等待签到接口回复失败：{redact(exc)}")
        for name, locator in (("按钮文案", page.locator(CHECKIN_BUTTON).first),
                              ("checkinResult", page.locator(".checkinResult").first)):
            try:
                log(f"现场 {name}={locator.inner_text(timeout=3_000)!r}")
            except Exception:
                pass
        text = _page_text(page)
        log(f"现场页面文案片段={text[:600]!r}")
        result = classify_page_state(text)
        if result.ok or result.status == "fatal":
            log(f"改用页面渲染结果判定：{result.status}（{result.message}）")
            return result
        # 还有一种情况：点击的正是已经签到的按钮（被禁用，不会发请求）。
        try:
            now_disabled = page.locator(CHECKIN_BUTTON).first.is_disabled(timeout=5_000)
        except Exception:
            now_disabled = False
        if now_disabled:
            log("点击后按钮已禁用，站点自身状态显示今日已完成，按“今日已签到”处理。")
            return CheckinResult("already", "按钮已禁用（今日已签到）", source="dom")
        raise RetryableError(f"点击签到按钮后没有拿到结果：{redact(exc)}") from exc


def _log_checkin_request(request):
    """记录所有 /api/user/checkin 请求（方法 + 是否在点击之后发出）。"""
    try:
        if CHECKIN_API.search(request.url):
            log(f"发起签到请求：{request.method} {request.url}")
    except Exception:
        pass


def _log_console_message(message):
    """只记录报错级别的前端日志，避免刷屏。"""
    try:
        if message.type in ("error", "warning"):
            log(f"页面 {message.type}：{message.text[:200]}")
    except Exception:
        pass


def _load_sync_playwright():
    """延迟导入 playwright，便于在没装浏览器依赖的环境里跑纯逻辑测试。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 环境缺失时给出明确指引
        raise FatalError(
            "没有安装 playwright，无法使用浏览器签到。请执行 "
            "pip install -r requirements-browser.txt 与 "
            "python -m playwright install --with-deps chromium。"
        ) from exc
    return sync_playwright


def run(cookie_header=None, headless=True, dry_run=False, max_attempts=MAX_ATTEMPTS,
        retry_delay=RETRY_DELAY_SECONDS, apply_jitter=True, channel=None):
    """打开浏览器完成签到，返回 (exit_code, CheckinResult)。"""
    use_email = email_mode()
    # dry-run 保持只读，不发送验证码。
    if use_email and dry_run:
        raise FatalError("邮箱模式的 --dry-run 不会申请验证码；请用 --no-jitter 测试完整登录签到。")
    if use_email:
        QQMailbox()  # 先校验配置，避免随机等待后才发现缺少邮箱参数；此处不连接邮箱。
    sync_playwright = _load_sync_playwright()
    cookie = None if use_email else (cookie_header or require_cookie())
    kind = session_cookie_kind(cookie) if cookie else None
    if not use_email and kind != "gld":
        log("WARNING: Cookie 未包含完整 gld:sess 与 gld:sess.sig，新版接口很可能会拒绝。")

    if apply_jitter and not dry_run:
        min_minutes = get_env("GLADOS_JITTER_MIN_MINUTES", "0")
        max_minutes = get_env("GLADOS_JITTER_MAX_MINUTES", "90")
        if get_env("GH_EVENT") == "workflow_dispatch":
            log("检测到手动触发（workflow_dispatch），跳过随机等待，立即签到。")
        else:
            sleep_with_jitter(int(min_minutes), int(max_minutes))
    elif dry_run:
        log("dry-run：跳过随机等待，也不点击签到按钮。")

    browser_channel = get_env("GLADOS_BROWSER_CHANNEL") or (channel or "")
    log(f"启动 Chromium（playwright {_playwright_version()}，headless={headless}，"
        f"通道={browser_channel or 'playwright 自带 chromium'}）")
    with sync_playwright() as playwright:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        if headless:
            launch_args.append("--headless=new")
        launch_kwargs = {"headless": headless, "args": launch_args}
        if browser_channel:
            # 本机调试时可用已安装的 Chrome/Edge，免去下载 Chromium。
            launch_kwargs["channel"] = browser_channel
        browser = playwright.chromium.launch(**launch_kwargs)
        try:
            context_options = {
                "viewport": {"width": 1440, "height": 900},
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
            }
            user_agent = None if use_email else _user_agent()
            if user_agent:
                context_options["user_agent"] = user_agent
            context = browser.new_context(**context_options)
            fingerprint = "" if use_email else get_fingerprint()
            if fingerprint:
                _install_fingerprint(context, fingerprint)
                log(f"已注入设备指纹 {fingerprint[:12]}…（来源 GLADOS_FP，共 {len(fingerprint)} 字符）")
            elif not use_email:
                log("WARNING: 未配置 GLADOS_FP，将使用 runner 实时计算的指纹；"
                    "服务端大概率返回 device-mismatch。请用 print_fingerprint.py 从登录设备导出。")
            if cookie:
                context.add_cookies(_cookie_list(cookie))
                log(f"已注入 {len(parse_cookie_pairs(cookie))} 个 Cookie 字段（不显示值）。")
            try:
                page = context.new_page()
                if use_email:
                    log("邮箱模式：使用当前浏览器生成的设备信息，忽略外部 Cookie、FP 和 UA。")
                    login_with_email(page)
                # 登录页可能打印验证码回复，仅在完成登录后注册日志。
                page.on("request", _log_checkin_request)
                page.on("dialog", lambda dialog: dialog.dismiss())
                page.on("console", _log_console_message)
                if dry_run:
                    return _dry_run(page)
                result = _checkin_with_retry(page, max_attempts, retry_delay)
                if result.ok:
                    read_account(page)
                else:
                    _screenshot(page, "checkin-failed")
                return (EXIT_OK if result.ok else _exit_code(result.status)), result
            finally:
                context.close()
        finally:
            browser.close()


def _dry_run(page):
    log(f"打开签到页：{CHECKIN_URL}")
    page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    log(f"页面标题={page.title()!r}，当前地址={page.url}")
    if "/login" in page.url or "/console" not in page.url:
        _screenshot(page, "dry-run")
        log(f"dry-run 结果：会话未生效（被重定向到 {page.url}），Cookie 需要重新获取。")
        return EXIT_FATAL, CheckinResult("fatal", f"dry-run 会话未生效（{page.url}）",
                                         source="dry-run")
    try:
        page.wait_for_selector(CHECKIN_BUTTON, timeout=SELECTOR_TIMEOUT_MS)
    except Exception:
        _screenshot(page, "dry-run")
        log(f"dry-run 结果：未找到签到按钮（当前地址={page.url}），请查看截图确认页面状态。")
        return EXIT_FATAL, CheckinResult("fatal", "dry-run 未找到签到按钮", source="dry-run")
    exists, disabled, label = _button_state(page)
    log(f"dry-run 结果：登录态正常，签到按钮文案={label!r}，disabled={disabled}（未点击）。")
    if disabled:
        log(f"页面文案片段：{_page_text(page, 400)!r}")
    return EXIT_OK, CheckinResult("success", "dry-run 完成", source="dry-run")


def _checkin_with_retry(page, max_attempts, retry_delay):
    result = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            result = _attempt(page, None)
        except RetryableError as exc:
            result = CheckinResult("retryable", str(exc))
        if result.ok:
            return result
        if result.status == "fatal":
            return result
        if attempt < max_attempts:
            log(f"第 {attempt}/{max_attempts} 次签到未成功：{result.message}；{retry_delay} 秒后重试。")
            time.sleep(retry_delay)
    return result


def _exit_code(status):
    return EXIT_FATAL if status == "fatal" else EXIT_RETRYABLE


def _fatal_hint(result):
    message = result.message or "签到失败"
    if email_mode():
        return ("邮箱模式已在同一浏览器内登录和签到。如仍出现设备校验或自动化拒绝，"
                "自动重登未必能解决，请在固定设备上核实站点要求；不要反复申请验证码。")
    lowered = message.lower()
    if DEVICE_MISMATCH_MARKER in lowered or "登录设备" in message or "automated" in lowered:
        return (
            "GLaDOS 用浏览器里的设备指纹（localStorage 的 glados.fp.v1，作为 Authorization "
            "请求头发送）校验登录设备，runner 上算出来的指纹与登录设备不同，所以被拒。"
            "请在登录 GLaDOS 的那台电脑上执行："
            "& .venv\\Scripts\\python.exe print_fingerprint.py，"
            "把输出的 fp1.… 存为仓库 Secret GLADOS_FP，同时确认 GLADOS_USER_AGENT 是同一浏览器的完整 UA。"
        )
    if any(marker in message.lower() for marker in AUTH_ERROR_MARKERS) or "没有权限" in message:
        return (
            "Cookie 未被接受。请确认 GLADOS_COOKIE 是登录后从成功的 /api/user/status 请求里"
            "复制的完整单行 Cookie，并且同时含 gld:sess 与 gld:sess.sig。"
        )
    return "请查看上方日志与 artifacts 里的失败截图。"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="用真实浏览器完成 GLaDOS 签到")
    parser.add_argument("--dry-run", action="store_true",
                        help="只检查登录态与按钮，不等待随机时间、不点击")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（本机调试用）")
    parser.add_argument("--no-jitter", action="store_true", help="跳过随机等待")
    parser.add_argument("--timeout-seconds", type=int, default=None,
                        help="随机等待上限覆盖值（分钟）")
    parser.add_argument("--channel", default=None,
                        help="使用已安装的浏览器通道（如 chrome / msedge），"
                             "省去下载 Playwright Chromium")
    return parser.parse_args(argv)


def apply_arg_overrides(args):
    if args.timeout_seconds is not None:
        os.environ["GLADOS_JITTER_MAX_MINUTES"] = str(args.timeout_seconds)


def main(argv=None):
    args = parse_args(argv)
    apply_arg_overrides(args)

    try:
        code, result = run(headless=not args.headed, dry_run=args.dry_run,
                           apply_jitter=not args.no_jitter, channel=args.channel)
    except FatalError as exc:
        log(f"ERROR: {exc}")
        return EXIT_FATAL
    except RetryableError as exc:
        log(f"ERROR: {exc}")
        return EXIT_RETRYABLE

    if result.ok:
        log(f"签到完成：{result.message or '<无 message>'}（来源={result.source}）")
        return code
    log(f"ERROR: 签到失败：{result.message}")
    log(f"处理建议：{_fatal_hint(result)}")
    return code


if __name__ == "__main__":
    sys.exit(main())

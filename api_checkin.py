"""复用用户现有 Cookie 调用签到 API；不重新登录，不申请验证码。"""
import re
import time

from browser_checkin import CheckinResult, classify_payload, get_fingerprint
from email_login import email_mode
from glados_cookie import (FatalError, RetryableError, SECRET_VALUES, get_env, log, normalize_cookie,
                           parse_cookie_pairs, require_cookie, require_supported_session)

ORIGIN = "https://glados.cloud"
DEFAULT_TOKEN = "glados.cloud"
MAX_ATTEMPTS = 2


def resolve_backend():
    if email_mode():
        return "browser"
    method = get_env("GLADOS_CHECKIN_METHOD", "api").lower()
    if method not in ("api", "browser"):
        raise FatalError("GLADOS_CHECKIN_METHOD 只支持 api 或 browser。")
    return method


def _load_requests():
    try:
        import requests
    except ImportError:
        raise FatalError("Cookie 接口模式需要 requests，请安装 requirements.txt。") from None
    return requests


def resolve_token():
    token = get_env("GLADOS_CHECKIN_TOKEN", DEFAULT_TOKEN)
    if token == "glados.one":
        log("GLADOS_CHECKIN_TOKEN 是旧值 glados.one，改用 glados.cloud。")
        return DEFAULT_TOKEN
    return token


def _json_request(session, requests, method, path, payload=None):
    try:
        response = session.request(method, ORIGIN + path, json=payload,
                                   timeout=(10, 30), allow_redirects=False)
    except requests.RequestException:
        raise RetryableError("连接 GLaDOS API 失败或超时。") from None
    status = response.status_code
    log(f"{method.upper()} {path}：HTTP {status}。")
    if status == 429 or status >= 500:
        raise RetryableError(f"GLaDOS API 暂不可用或限流（HTTP {status}）。")
    if 300 <= status < 400:
        raise FatalError("GLaDOS API 返回重定向，停止请求；未把 Cookie 转发到其他地址。")
    if status in (401, 403):
        raise FatalError(f"GLaDOS API 拒绝请求（HTTP {status}），请检查会话或站点验证要求。")
    if status >= 400:
        raise FatalError(f"GLaDOS API 返回 HTTP {status}。")
    try:
        data = response.json()
    except ValueError:
        raise FatalError("GLaDOS API 未返回有效 JSON；可能是错误页或验证页，未确认签到成功。") from None
    if not isinstance(data, dict):
        raise FatalError("GLaDOS API 回复格式异常，未确认签到成功。")
    return data


def classify_checkin(data):
    if data.get("captcha_required"):
        return CheckinResult("fatal", "签到接口要求人机验证，需要人工处理。", source="api-cookie")
    if data.get("reason") == "device-mismatch":
        return CheckinResult("fatal", "签到接口返回 device-mismatch；请使用同一已登录浏览器的 Cookie、"
                             "glados.fp.v1 和 User-Agent 配置 GLADOS_COOKIE、GLADOS_FP、GLADOS_USER_AGENT。",
                             source="api-cookie")
    message = str(data.get("message", ""))
    if "please checkin via" in message.lower():
        return CheckinResult("fatal", "签到 token 不被接受，请核对 GLADOS_CHECKIN_TOKEN；默认是 glados.cloud。",
                             source="api-cookie")
    result = classify_payload(data)
    result.source = "api-cookie"
    if result.status == "fatal":
        result.message = (message or "会话未被接受") + "；请在浏览器手动登录后更新完整 GLADOS_COOKIE。"
    elif not result.ok:
        # 未知业务回复不盲目重复提交；HTTP/网络临时故障才重试。
        result.status = "fatal"
        result.message = message or "未知签到回复，未确认成功；请核对站点实际积分。"
    return result


def _report_account(session, requests):
    for path, field in (("/api/user/status", "leftDays"), ("/api/user/points", "points")):
        try:
            data = _json_request(session, requests, "get", path)
            if data.get("code") not in (0, None):
                log(f"WARNING: {path} 未确认有效账号信息；签到结果保持成功。")
                continue
            container = data.get("data") if isinstance(data.get("data"), dict) else data
            value = container.get(field)
            if isinstance(value, (str, int, float)) and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", str(value)):
                log(f"{field}={value}")
        except (FatalError, RetryableError):
            log(f"WARNING: {path} 查询失败；签到结果保持成功。")


def run(cookie_header=None, dry_run=False, max_attempts=MAX_ATTEMPTS, retry_delay=5):
    requests = _load_requests()
    cookie = normalize_cookie(cookie_header) if cookie_header else require_cookie()
    require_supported_session(cookie)
    SECRET_VALUES.add(cookie)
    SECRET_VALUES.update(value for _, value in parse_cookie_pairs(cookie))
    fingerprint = get_fingerprint()
    if fingerprint:
        SECRET_VALUES.add(fingerprint)
    with requests.Session() as session:
        session.headers.update({"Accept": "application/json, text/plain, */*", "Cookie": cookie,
                                "Origin": ORIGIN, "Referer": ORIGIN + "/console/checkin"})
        if get_env("GLADOS_USER_AGENT"):
            session.headers["User-Agent"] = get_env("GLADOS_USER_AGENT")
        if fingerprint:
            session.headers["Authorization"] = fingerprint
        log("Cookie 接口模式：立即签到，复用已有会话，不发送邮箱验证码。")
        if dry_run:
            data = _json_request(session, requests, "get", "/api/user/status")
            if data.get("code") == 0:
                return 0, CheckinResult("success", "dry-run：服务端确认 Cookie 有效，未发送签到请求。", source="api-cookie")
            return 1, CheckinResult("fatal", "dry-run：服务端未接受 Cookie，请重新导出完整会话。", source="api-cookie")
        result = None
        for attempt in range(1, max(1, max_attempts) + 1):
            try:
                data = _json_request(session, requests, "post", "/api/user/checkin", {"token": resolve_token()})
                result = classify_checkin(data)
                break
            except RetryableError as exc:
                result = CheckinResult("retryable", str(exc), source="api-cookie")
                if attempt < max_attempts:
                    log(f"签到 API 临时故障，{retry_delay} 秒后重试（{attempt}/{max_attempts}）。")
                    time.sleep(retry_delay)
        if result.ok:
            log(f"签到完成：{result.message or '服务端返回成功'}（Cookie API）。")
            _report_account(session, requests)
        else:
            log(f"ERROR: {result.message}")
        return (0 if result.ok else 1 if result.status == "fatal" else 2), result

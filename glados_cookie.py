"""GLaDOS Cookie 解析、校验与脱敏日志。

2026-09 版 GLaDOS 会话使用 gld:sess + gld:sess.sig；旧的 koa:sess 会话在新版
API 上会返回 {"code":-2,"message":"没有权限"}。本模块把两种格式都解析出来，
但只把含完整 gld 会话的 Cookie 视为可用，并在明显是旧版/截断内容时提前失败，
避免向服务端发送注定被拒绝的凭据。
"""
import hashlib
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone

BEIJING_TZ = timezone(timedelta(hours=8))

CURRENT_SESSION_COOKIES = ("gld:sess", "gld:sess.sig")
LEGACY_SESSION_COOKIES = ("koa:sess", "koa:sess.sig")

# 真实 gld 会话的值：gld:sess 约 52 字符、gld:sess.sig 约 29 字符（实测）。
# 远低于这个下限说明复制被截断，但绝不能因为"整串短"就拒绝合法 Cookie。
MIN_PLAUSIBLE_COOKIE_VALUE = 12
SHORT_VALUE_WARNING = 24

COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_\x60|~0-9A-Za-z:]+$")
RAW_VALUE = re.compile(r"^[A-Za-z0-9_+/-]+={0,2}$")


class FatalError(RuntimeError):
    """凭据/配置/接口回复需要人工处理。"""


class RetryableError(RuntimeError):
    """临时网络或服务端故障。"""


SECRET_VALUES = set()


def get_env(name, default=""):
    return os.getenv(name, "").strip() or default


def redact(text):
    value = str(text)
    for secret in sorted(SECRET_VALUES, key=len, reverse=True):
        if len(secret) >= 4:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?:gld|koa):sess(?:\.sig)?=[^;\s\"']+", "sess=[REDACTED]", value)
    value = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", value, flags=re.I)
    return re.sub(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", value
    )


def log(message):
    now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S UTC+08:00")
    print(f"[{now}] {redact(message)}", flush=True)


def validate_cookie_header(cookie):
    # 只做校验，不解析/重排带签名的值，也不丢字段。
    if any(ord(char) < 32 or ord(char) >= 127 for char in cookie):
        raise FatalError("Cookie 请求头包含换行或非 ASCII 字符，请复制 Network 中的 Cookie 值。")
    for part in cookie.split(";"):
        if not part.strip():
            continue
        name, separator, value = part.strip().partition("=")
        if not separator or not COOKIE_NAME.fullmatch(name) or re.search(r"\s", value):
            raise FatalError("Cookie 格式无效：需要 name=value; name=value，请勿粘贴 Set-Cookie 属性。")
    return cookie


def normalize_cookie(raw):
    """保留完整请求头；只转换明确不同的输入格式。"""
    cookie = raw.strip()
    if not cookie:
        raise FatalError("GLADOS_COOKIE 为空，请检查当前仓库的 Repository Secret。")
    if len(cookie) >= 2 and (cookie[0], cookie[-1]) in (
        ('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’"),
    ):
        cookie = cookie[1:-1].strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()

    if cookie.startswith(("[", "{")):
        try:
            exported = json.loads(cookie)
        except ValueError as exc:
            raise FatalError("Cookie JSON 无效，请使用 Cookie-Editor 导出的 JSON。") from exc
        if isinstance(exported, dict) and isinstance(exported.get("cookie"), str):
            return normalize_cookie(exported["cookie"])
        if isinstance(exported, dict) and "name" in exported and "value" in exported:
            exported = [exported]
        if not isinstance(exported, list) or not exported:
            raise FatalError("需要单个账号的 Cookie-Editor JSON 数组或完整 Cookie 请求头。")
        pairs, names, domains = [], set(), set()
        for item in exported:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise FatalError("Cookie-Editor 每项必须包含 name 和 value。")
            if not isinstance(item.get("value"), str):
                raise FatalError("Cookie-Editor 的 value 必须是字符串。")
            name = item["name"]
            if name in names:
                raise FatalError("Cookie JSON 有重复名称，请只导出实际登录域名的 Cookie。")
            names.add(name)
            domain = item.get("domain")
            if isinstance(domain, str) and domain:
                domains.add(domain.lstrip(".").lower())
            pairs.append(f"{name}={item['value']}")
        if len(domains) > 1:
            raise FatalError("Cookie JSON 包含多个域名，请只导出一个登录域名。")
        return validate_cookie_header("; ".join(pairs))

    # 只在用户原始的两值格式里解码 HTML 空格。
    parts = html.unescape(cookie).split()
    if len(parts) == 2 and all(RAW_VALUE.fullmatch(part) for part in parts):
        return validate_cookie_header(f"koa:sess={parts[0]}; koa:sess.sig={parts[1]}")

    # 完整请求头包含不透明签名数据：绝不 HTML/URL/base64 解码、重编码或重排。
    if re.match(r"^[!#$%&'*+\-.^_\x60|~0-9A-Za-z:]+=", cookie):
        return validate_cookie_header(cookie)
    raise FatalError("Cookie 无法识别：使用完整 name=value 请求头、Cookie-Editor JSON 或两个原始会话值。")


def parse_cookie_pairs(cookie):
    """把 Cookie 请求头拆成 (name, value) 列表，供浏览器上下文注入。"""
    pairs = []
    for part in cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            pairs.append((name, value))
    return pairs


def session_cookie_kind(cookie):
    names = {name for name, _ in parse_cookie_pairs(cookie)}
    if set(CURRENT_SESSION_COOKIES).issubset(names):
        return "gld"
    if set(LEGACY_SESSION_COOKIES).issubset(names):
        return "koa"
    return None


def require_current_session(cookie):
    """新版 API 只接受完整 gld 会话，旧版/截断内容提前给出可操作提示。"""
    kind = session_cookie_kind(cookie)
    names = [name for name, _ in parse_cookie_pairs(cookie)]
    if kind == "gld":
        # 字段名对了，再看值本身是否被截断（只看值长度，不看整串长度）。
        for name, value in parse_cookie_pairs(cookie):
            if name in CURRENT_SESSION_COOKIES and len(value) < MIN_PLAUSIBLE_COOKIE_VALUE:
                raise FatalError(
                    f"Cookie 里 {name} 的值只有 {len(value)} 个字符，不像完整会话值"
                    f"（实测 gld:sess 约 52、gld:sess.sig 约 29 字符），"
                    "基本可以确定复制被截断。请在 Network 里右键 Cookie → Copy value 整行粘贴。"
                )
            if name in CURRENT_SESSION_COOKIES and len(value) < SHORT_VALUE_WARNING:
                log(f"WARNING: {name} 的值偏短（{len(value)} 字符），如果签到失败请重新复制 Cookie。")
        return "gld"
    if kind == "koa":
        raise FatalError(
            "Cookie 只有旧版 koa:sess 会话，GLaDOS 2026 新版接口会返回“没有权限”。"
            "请在 glados.cloud 重新登录，从成功的 /api/user/status 请求里复制含 "
            "gld:sess 与 gld:sess.sig 的完整单行 Cookie 后更新 GLADOS_COOKIE。"
        )
    raise FatalError(
        "Cookie 缺少 gld:sess 或 gld:sess.sig，无法通过 GLaDOS 2026 新版认证。"
        f"当前字段（共 {len(cookie)} 字节）：{', '.join(names) if names else '<无>'}。"
    )


def require_cookie():
    raw = os.getenv("GLADOS_COOKIE", "")
    SECRET_VALUES.add(raw.strip())
    cookie = normalize_cookie(raw)
    SECRET_VALUES.add(cookie)
    for _, value in parse_cookie_pairs(cookie):
        SECRET_VALUES.add(value)
    kind = session_cookie_kind(cookie)
    raw_fingerprint = hashlib.sha256(raw.strip().encode()).hexdigest()[:12]
    fingerprint = hashlib.sha256(cookie.encode()).hexdigest()[:12]
    log(f"Cookie 输入长度={len(raw.strip())}，输入指纹={raw_fingerprint}；"
        f"请求头长度={len(cookie)}，请求头指纹={fingerprint}；会话类型={kind or '未识别'}。")
    require_current_session(cookie)
    return cookie

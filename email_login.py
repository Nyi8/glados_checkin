"""在签到浏览器内登录；QQ IMAP 只读获取本次申请后的邮件，不保存凭据。"""
import imaplib
import json
import re
import time
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from urllib.parse import urlsplit

from glados_cookie import FatalError, RetryableError, SECRET_VALUES, get_env, log

ORIGIN = "https://glados.cloud"
PAGE_ATTEMPTS = 2
PAGE_TIMEOUT_MS = 30_000
FORM_TIMEOUT_MS = 45_000


def email_mode():
    mode = get_env("GLADOS_AUTH_MODE", "cookie").lower()
    if mode not in ("cookie", "email"):
        raise FatalError("GLADOS_AUTH_MODE 只支持 cookie 或 email。")
    return mode == "email"


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("br", "p", "div", "td"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def extract_code(raw, sender, recipient, pattern):
    """发件人必须精确匹配，收件人必须匹配；验证码有歧义则不提交。"""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    addresses = lambda key: {address.lower() for _, address in
                             getaddresses(message.get_all(key, []))}
    if addresses("From") != {sender.lower()} or recipient.lower() not in addresses("To"):
        return None
    texts = [str(message.get("Subject", ""))]
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            continue
        if part.get_content_type() == "text/html":
            parser = _HTMLText()
            parser.feed(body)
            body = "".join(parser.parts)
        texts.append(body)
    codes = set()
    for match in pattern.finditer("\n".join(texts)):
        code = match.group(1) if pattern.groups else match.group(0)
        if code and re.fullmatch(r"[A-Za-z0-9-]{4,32}", code):
            codes.add(code)
    if len(codes) != 1:
        return None
    code = codes.pop()
    SECRET_VALUES.add(code)
    return code


class QQMailbox:
    def __init__(self):
        self.address = get_env("GLADOS_EMAIL").lower()
        self.username = get_env("QQ_IMAP_USERNAME", self.address)
        self.password = get_env("QQ_IMAP_AUTH_CODE")
        SECRET_VALUES.update((self.address, self.username, self.password))
        self.sender = get_env("GLADOS_MAIL_FROM").lower()
        if not all((self.address, self.username, self.password, self.sender)):
            raise FatalError("邮箱模式需要 GLADOS_EMAIL、QQ_IMAP_AUTH_CODE、GLADOS_MAIL_FROM"
                             "（验证码邮件的完整发件地址）；QQ_IMAP_USERNAME 可选。")
        for address in (self.address, self.username, self.sender):
            if not re.fullmatch(r"[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+", address):
                raise FatalError("邮箱配置必须填写完整邮件地址，不含显示名。")
        try:
            self.pattern = re.compile(get_env("GLADOS_CODE_REGEX", r"\b[0-9]{6}\b"))
            self.timeout = int(get_env("GLADOS_MAIL_TIMEOUT_SECONDS", "180"))
            if not 15 <= self.timeout <= 600 or self.pattern.groups > 1:
                raise ValueError()
        except (ValueError, re.error) as exc:
            raise FatalError("验证码配置无效：超时需为 15–600 秒，正则最多一个捕获组。") from exc
        self.client = None
        self.seen = set()
        self.uidvalidity = None

    def __enter__(self):
        try:
            import ssl

            self.client = imaplib.IMAP4_SSL("imap.qq.com", 993,
                                          ssl_context=ssl.create_default_context(), timeout=20)
            self.client.login(self.username, self.password)
            status, _ = self.client.select("INBOX", readonly=True)
            if status != "OK":
                raise RetryableError("无法以只读方式打开 QQ 收件箱。")
            self.uidvalidity = self.client.response("UIDVALIDITY")[1]
            # 点击发送之前记录所有已有 UID；不能只查未读邮件。
            self.seen = set(self._uids())
            return self
        except imaplib.IMAP4.abort as exc:
            self.__exit__(None, None, None)
            raise RetryableError("QQ IMAP 连接中断，本次未发送验证码。") from exc
        except imaplib.IMAP4.error as exc:
            self.__exit__(None, None, None)
            raise FatalError("QQ IMAP 登录失败；请开启 IMAP 并使用邮箱授权码。") from exc
        except Exception as exc:
            self.__exit__(None, None, None)
            if isinstance(exc, (FatalError, RetryableError)):
                raise
            raise RetryableError("连接 QQ IMAP 失败，请检查网络与 TLS 支持。") from exc

    def __exit__(self, *args):
        if self.client:
            try:
                self.client.logout()
            except Exception:
                pass

    def _uids(self):
        status, data = self.client.uid("search", None, "ALL")
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise RetryableError("QQ IMAP 搜索邮件失败。")
        return data[0].split()

    def wait_for_code(self, requested_at):
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline:
                status, _ = self.client.select("INBOX", readonly=True)
                if status != "OK" or self.client.response("UIDVALIDITY")[1] != self.uidvalidity:
                    raise RetryableError("收件箱标识已变化，停止本次验证码登录。")
                new = set(self._uids()) - self.seen
                for uid in sorted(new, key=int, reverse=True):
                    status, data = self.client.uid("fetch", uid, "(INTERNALDATE BODY.PEEK[])")
                    if status != "OK":
                        continue
                    self.seen.add(uid)
                    for item in data or []:
                        if not isinstance(item, tuple) or len(item) != 2:
                            continue
                        received = imaplib.Internaldate2tuple(item[0])
                        if received is None or time.mktime(received) < requested_at - 5:
                            continue
                        code = extract_code(item[1], self.sender, self.address, self.pattern)
                        if code:
                            log("已读取本次登录验证码（不显示内容）。")
                            return code
                time.sleep(min(5, max(0, deadline - time.monotonic())))
        except (imaplib.IMAP4.error, OSError) as exc:
            raise RetryableError("读取 QQ 验证码邮件失败，停止本次登录。") from exc
        raise RetryableError("等待验证码超时；请检查收件箱、发件地址与验证码格式配置。")


def _is_response(response, path):
    url = urlsplit(response.url)
    return (url.scheme == "https" and url.netloc == "glados.cloud" and url.path == path
            and response.request.method.upper() == "POST")


def _check_reply(response):
    try:
        data = response.json()
    except Exception as exc:
        raise RetryableError("登录接口返回非 JSON 内容。") from exc
    if response.status == 429 or response.status >= 500:
        raise RetryableError("登录接口限流或暂不可用，本次不重复申请验证码。")
    if not isinstance(data, dict):
        raise RetryableError("登录接口回复格式异常。")
    if data.get("captcha_required"):
        raise FatalError("GLaDOS 要求人机验证，需要人工登录；本次停止自动登录。")
    if response.status >= 400 or data.get("code") != 0:
        # 站点回复可能包含验证码/登录信息，不直接输出。
        raise FatalError("GLaDOS 拒绝邮箱登录；请检查账号、验证码格式或账号迁移状态。")


def _network_error(error):
    """只保留浏览器固定的网络错误码；不输出异常中的 fill 参数或 URL 查询串。"""
    match = re.search(r"\bnet::ERR_[A-Z0-9_]{1,64}\b", str(error))
    if match:
        return match.group(0)
    return "TimeoutError" if isinstance(error, TimeoutError) or type(error).__name__ == "TimeoutError" else "页面异常"


class _LoginDiagnostics:
    """登录诊断只记录固定资源分类与数字状态码，不记录页面/响应正文。"""
    PATHS = {"/login": "login-document", "/app.bundle.js": "app-script",
             "/api/user/session": "session-api", "/api/authorization": "send-code-api",
             "/api/login": "login-api"}

    def __init__(self, page):
        self.page = page
        self.stage = "打开登录页"
        self.statuses = {}
        self.failed = []
        self.script_errors = 0
        self.handlers = [("response", self._response), ("requestfailed", self._failed),
                         ("pageerror", self._page_error)]

    def start(self):
        for event, handler in self.handlers:
            self.page.on(event, handler)

    def close(self):
        for event, handler in self.handlers:
            try:
                self.page.remove_listener(event, handler)
            except Exception:
                pass

    def _resource(self, url):
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.netloc == "glados.cloud":
            return self.PATHS.get(parsed.path, "other-site-resource")
        return "external-resource"

    def _response(self, response):
        try:
            resource = self._resource(response.url)
            if resource in self.PATHS.values() and isinstance(response.status, int):
                self.statuses[resource] = response.status
        except Exception:
            pass

    def _failed(self, request):
        try:
            self.failed.append(self._resource(request.url) + ":" + _network_error(request.failure))
            self.failed = self.failed[-5:]
        except Exception:
            pass

    def _page_error(self, error):
        self.script_errors += 1

    def summary(self):
        try:
            parsed = urlsplit(self.page.url)
            location = self.PATHS.get(parsed.path, "other-site-page") if (
                parsed.scheme == "https" and parsed.netloc == "glados.cloud") else "other-page"
        except Exception:
            location = "unknown-page"
        return json.dumps({"stage": self.stage, "page": location, "http": self.statuses,
                           "failed": self.failed, "script_errors": self.script_errors}, ensure_ascii=False)


def _require_login_page(page):
    parsed = urlsplit(page.url)
    if parsed.scheme != "https" or parsed.netloc != "glados.cloud" or parsed.path != "/login":
        raise FatalError("登录页跳转到了其他地址，停止提交邮箱或验证码。")


def _open_login_page(page, diagnostics):
    """仅重试尚未提交邮箱的页面 GET；不等待外部资源完成加载。"""
    for attempt in range(1, PAGE_ATTEMPTS + 1):
        try:
            diagnostics.stage = "打开登录页"
            log(f"访问 GLaDOS 登录页（第 {attempt}/{PAGE_ATTEMPTS} 次，等待主文档响应）。")
            response = page.goto(ORIGIN + "/login", wait_until="commit", timeout=PAGE_TIMEOUT_MS)
            if response:
                diagnostics._response(response)
            status = response.status if response else None
            if isinstance(status, int):
                log(f"登录页主文档 HTTP {status}。")
                if status in (401, 403, 429):
                    raise FatalError(f"登录页 HTTP {status}，站点拒绝访问或限流；停止本次登录。")
                if status >= 400:
                    raise RetryableError(f"登录页 HTTP {status}。")
            diagnostics.stage = "等待邮箱输入框"
            page.locator("#email").wait_for(state="visible", timeout=FORM_TIMEOUT_MS)
            _require_login_page(page)
            log("登录表单已就绪。")
            return
        except FatalError:
            raise
        except Exception as exc:
            detail = f"{diagnostics.summary()}；{_network_error(exc)}"
            if attempt == PAGE_ATTEMPTS:
                raise RetryableError(f"登录页尚未就绪，未申请验证码；{detail}") from None
            log(f"登录页访问失败，将在未发送验证码的情况下重试一次；{detail}")
            time.sleep(1)


def login(page):
    """仅操作当前站点的登录表单，同一 context 随后用于签到。"""
    with QQMailbox() as mailbox:
        log("QQ IMAP 已登录并以只读方式打开收件箱，邮箱授权码已通过本次连接验证。")
        diagnostics = _LoginDiagnostics(page)
        diagnostics.start()
        try:
            _open_login_page(page, diagnostics)
            diagnostics.stage = "填写邮箱"
            page.locator("#email").fill(mailbox.address, timeout=45_000)
            _require_login_page(page)
            requested_at = time.time()
            diagnostics.stage = "申请验证码"
            log("准备点击 Get Code；点击后不会自动重发验证码。")
            with page.expect_response(lambda r: _is_response(r, "/api/authorization"),
                                      timeout=45_000) as reply:
                page.get_by_role("button", name="Get Code", exact=True).click()
            _check_reply(reply.value)
            log("已通过 GLaDOS 登录页申请验证码，等待 QQ 邮箱收信。")
            code = mailbox.wait_for_code(requested_at)
            diagnostics.stage = "填写验证码"
            _require_login_page(page)
            page.locator("#mailcode").fill(code)
            diagnostics.stage = "提交登录"
            with page.expect_response(lambda r: _is_response(r, "/api/login"),
                                      timeout=45_000) as reply:
                page.get_by_role("button", name="Login", exact=True).click()
            _check_reply(reply.value)
            diagnostics.stage = "等待登录跳转"
            page.wait_for_url(ORIGIN + "/console**", wait_until="commit", timeout=45_000)
            log("邮箱登录成功，将在当前浏览器中签到。")
        except (FatalError, RetryableError):
            raise
        except Exception as exc:
            # Playwright 的异常会包含 fill 参数；验证码页也不能截图/记录控制台。
            raise RetryableError(f"邮箱登录页面操作失败；{diagnostics.summary()}；"
                                 f"{_network_error(exc)}；本次不重复发送验证码。") from None
        finally:
            diagnostics.close()

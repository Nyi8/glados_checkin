import unittest
from unittest.mock import Mock, patch

import browser_checkin
from browser_checkin import CheckinResult, classify_page_state, classify_payload, jitter_seconds


def ensure_playwright_available():
    """本机/CI 没装 playwright 时注入一个假模块，让 import 通过。"""
    try:
        import playwright.sync_api  # noqa: F401
        return False
    except ImportError:
        patcher = patch.dict("sys.modules", {
            "playwright": Mock(),
            "playwright.sync_api": Mock(),
        })
        patcher.start()
        return patcher


class FakeLocator:
    def __init__(self, count=1, text="Checkin", disabled=False, on_click=None):
        self._count = count
        self._text = text
        self._disabled = disabled
        self._on_click = on_click or (lambda: None)

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def inner_text(self, timeout=None):
        return self._text

    def is_disabled(self, timeout=None):
        return self._disabled

    def click(self, timeout=None):
        self._on_click()


class FakeContextManager:
    def __init__(self, value=None, error=None):
        self._value = value
        self._error = error

    def __enter__(self):
        if self._error is not None:
            raise self._error
        return self

    def __exit__(self, *exc_info):
        return False

    @property
    def value(self):
        if self._error is not None:
            raise self._error
        return self._value


class PlaywrightContextManager(FakeContextManager):
    """模拟 `with sync_playwright() as playwright:` 的进入语义。"""

    def __enter__(self):
        return self._value


class FakePage:
    def __init__(self, url="https://glados.cloud/console/checkin", title="GLaDOS",
                 count=1, text="Checkin", disabled=False, body_text="", response=None,
                 click_error=None, goto_error=None, selector_error=None):
        self.url = url
        self._title = title
        self._locator = FakeLocator(count, text, disabled)
        self._body_text = body_text
        # 默认给一个成功的签到响应，避免忘记传 response 时 _attempt 拿到 None。
        self._response = response if response is not None else FakeResponse()
        self._click_error = click_error
        self._goto_error = goto_error
        self._selector_error = selector_error
        self.goto_calls = []
        self.selector_waits = []
        self.request = Mock()

    def title(self):
        return self._title

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        if self._goto_error:
            raise self._goto_error

    def wait_for_load_state(self, state=None, timeout=None):
        return None

    def wait_for_selector(self, selector, timeout=None):
        self.selector_waits.append(selector)
        if self._selector_error:
            raise self._selector_error

    def locator(self, selector):
        return self._locator

    def expect_response(self, predicate, timeout=None):
        self.predicate = predicate
        if self._click_error:
            self._locator._on_click = lambda: (_ for _ in ()).throw(self._click_error)
        return FakeContextManager(self._response)

    def inner_text(self, selector, timeout=None):
        return self._body_text

    def screenshot(self, path=None, full_page=None):
        if path:
            with open(path, "wb") as handle:
                handle.write(b"png")

    def evaluate(self, *args, **kwargs):
        return None


class FakeResponse:
    def __init__(self, payload=None, status=200, *, raise_json=False):
        self._payload = payload if payload is not None else {"code": 0, "message": "Checkin! Got 12"}
        self.status = status
        self.ok = status < 400
        self.url = "https://glados.cloud/api/user/checkin"
        self._raise_json = raise_json
        self.request = Mock(method="POST")

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class ClassifyPayloadTests(unittest.TestCase):
    def test_success_and_repeat_are_ok(self):
        for payload in (
            {"code": 0, "message": "Checkin! Got 12 points"},
            {"code": 1, "message": "Checkin Repeats! Please Try Tomorrow"},
            {"code": 1, "message": "Today's observation logged. Return tomorrow for more points."},
        ):
            with self.subTest(payload=payload):
                result = classify_payload(payload)
                self.assertTrue(result.ok)
                self.assertEqual(result.source, "api")

    def test_permission_denied_is_fatal(self):
        result = classify_payload({"code": -2, "message": "没有权限"})
        self.assertEqual(result.status, "fatal")

    def test_device_mismatch_is_fatal_and_not_retried(self):
        result = classify_payload({"code": 4, "reason": "device-mismatch",
                                   "loginDevice": "a", "currentDevice": "b"})
        self.assertEqual(result.status, "fatal")
        self.assertIn("登录设备", browser_checkin._fatal_hint(result))

    def test_non_dict_payload_is_retryable(self):
        self.assertEqual(classify_payload(["nope"]).status, "retryable")

    def test_automation_detection_is_retryable(self):
        self.assertEqual(classify_payload({"code": 1, "message": "Automated check-in detected"}).status,
                         "retryable")

    def test_page_state_fallbacks(self):
        self.assertTrue(classify_page_state("Checkin repeats! Please try tomorrow").ok)
        self.assertEqual(classify_page_state("没有权限").status, "fatal")
        self.assertEqual(classify_page_state("").status, "retryable")


class JitterTests(unittest.TestCase):
    def test_zero_width_window(self):
        self.assertEqual(jitter_seconds(0, 0), 0.0)

    def test_window_bounds_are_respected(self):
        for _ in range(200):
            seconds = jitter_seconds(0, 90)
            self.assertGreaterEqual(seconds, 0)
            self.assertLessEqual(seconds, 90 * 60)

    def test_invalid_window_is_rejected(self):
        for window in ((-1, 10), (10, 5), (0, browser_checkin.MAX_JITTER_MINUTES + 1)):
            with self.subTest(window=window), self.assertRaises(browser_checkin.FatalError):
                jitter_seconds(*window)

    def test_sleep_uses_the_computed_delay(self):
        with patch.object(browser_checkin.random.SystemRandom, "uniform", return_value=600), \
                patch.object(browser_checkin.time, "sleep") as sleep:
            self.assertEqual(browser_checkin.sleep_with_jitter(0, 90), 600)
        sleep.assert_called_once_with(600)


class AttemptTests(unittest.TestCase):
    def setUp(self):
        self.logging = patch.object(browser_checkin, "log")
        self.logging.start()
        self.addCleanup(self.logging.stop)

    def test_click_captures_api_response(self):
        page = FakePage(response=FakeResponse())
        result = browser_checkin._attempt(page, None)
        self.assertEqual(result.status, "success")
        self.assertEqual(page.selector_waits, [browser_checkin.CHECKIN_BUTTON])

    def test_login_redirect_is_fatal_without_clicking(self):
        page = FakePage(url="https://glados.cloud/login")
        with self.assertRaises(browser_checkin.FatalError):
            browser_checkin._attempt(page, None)
        self.assertEqual(page.selector_waits, [])

    def test_marketing_redirect_is_fatal_without_clicking(self):
        # 实测：无效会话访问 /console/checkin 会被弹回官网首页。
        page = FakePage(url="https://glados.cloud/", title="glados.cloud")
        with self.assertRaises(browser_checkin.FatalError) as raised:
            browser_checkin._attempt(page, None)
        self.assertIn("glados.cloud/", str(raised.exception))
        self.assertEqual(page.selector_waits, [])

    def test_missing_button_is_retryable(self):
        page = FakePage(selector_error=TimeoutError("no button"), body_text="Loading")
        with self.assertRaises(browser_checkin.RetryableError):
            browser_checkin._attempt(page, None)

    def test_disabled_button_with_repeat_text_is_already(self):
        page = FakePage(disabled=True, text="Checkin", body_text="Checkin repeats, try tomorrow")
        result = browser_checkin._attempt(page, None)
        self.assertEqual(result.status, "already")
        self.assertEqual(result.source, "dom")

    def test_permission_denied_is_fatal(self):
        page = FakePage(response=FakeResponse({"code": -2, "message": "没有权限"}))
        result = browser_checkin._attempt(page, None)
        self.assertEqual(result.status, "fatal")

    def test_click_timeout_falls_back_to_page_text(self):
        page = FakePage(click_error=TimeoutError("timeout"), body_text="没有权限")
        result = browser_checkin._attempt(page, None)
        self.assertEqual(result.status, "fatal")

    def test_click_timeout_without_result_is_retryable(self):
        page = FakePage(click_error=TimeoutError("timeout"), body_text="Loading")
        with self.assertRaises(browser_checkin.RetryableError):
            browser_checkin._attempt(page, None)

    def test_retry_uses_fresh_page_between_attempts(self):
        page = FakePage()
        results = [CheckinResult("retryable", "temporary"),
                   CheckinResult("success", "Checkin! Got 12")]
        with patch.object(browser_checkin, "_attempt", side_effect=results), \
                patch.object(browser_checkin.time, "sleep") as sleep:
            result = browser_checkin._checkin_with_retry(page, 2, 0)
        self.assertTrue(result.ok)
        sleep.assert_called_once_with(0)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.logging = patch.object(browser_checkin, "log")
        self.logging.start()
        self.addCleanup(self.logging.stop)

    def test_retryable_then_success_retries_with_delay(self):
        page = FakePage()
        results = [CheckinResult("retryable", "temporary"),
                   CheckinResult("success", "Checkin! Got 12")]
        with patch.object(browser_checkin, "_attempt", side_effect=results), \
                patch.object(browser_checkin.time, "sleep") as sleep:
            result = browser_checkin._checkin_with_retry(page, 2, 60)
        self.assertTrue(result.ok)
        sleep.assert_called_once_with(60)

    def test_fatal_result_is_not_retried(self):
        page = FakePage()
        with patch.object(browser_checkin, "_attempt",
                          return_value=CheckinResult("fatal", "没有权限")) as attempt, \
                patch.object(browser_checkin.time, "sleep") as sleep:
            result = browser_checkin._checkin_with_retry(page, 3, 60)
        self.assertEqual(result.status, "fatal")
        self.assertEqual(attempt.call_count, 1)
        sleep.assert_not_called()


class RunTests(unittest.TestCase):
    """覆盖 run() 的装配逻辑：Cookie 注入、退出码、截图与账号信息展示。"""

    def setUp(self):
        self.logging = patch.object(browser_checkin, "log")
        self.logging.start()
        self.addCleanup(self.logging.stop)
        self.screenshot = patch.object(browser_checkin, "_screenshot")
        self.screenshot_mock = self.screenshot.start()
        self.addCleanup(self.screenshot.stop)
        self.playwright_modules = ensure_playwright_available()
        if self.playwright_modules:
            self.addCleanup(self.playwright_modules.stop)

    def _fake_playwright(self, page_factory):
        playwright = Mock()
        browser = Mock()
        context = Mock()
        context.add_cookies = Mock()
        context.new_page = Mock(side_effect=page_factory)
        context.close = Mock()
        browser.new_context.return_value = context
        browser.close = Mock()
        playwright.chromium.launch.return_value = browser
        # sync_playwright() 返回的上下文管理器，进入后得到 playwright 对象，
        # 与真实 Playwright 的 `with sync_playwright() as p:` 语义保持一致。
        return PlaywrightContextManager(playwright), context

    def _patch_playwright(self, playwright_cm):
        # run() 里的用法是 `with sync_playwright() as playwright:`
        return patch.object(browser_checkin, "_load_sync_playwright",
                            return_value=Mock(return_value=playwright_cm))

    def test_success_run_injects_cookies_and_reports_account(self):
        page = FakePage(response=FakeResponse())
        playwright_cm, context = self._fake_playwright(lambda: page)
        cookie = "gld:sess=" + "A" * 180 + "; gld:sess.sig=" + "B" * 120
        with self._patch_playwright(playwright_cm), \
                patch.object(browser_checkin, "read_account") as read_account:
            code, result = browser_checkin.run(cookie_header=cookie, headless=True,
                                               apply_jitter=False)
        self.assertEqual(code, browser_checkin.EXIT_OK)
        self.assertTrue(result.ok)
        read_account.assert_called_once_with(page)
        self.assertEqual(len(context.add_cookies.call_args.args[0]), 2)

    def test_fatal_run_returns_exit_code_1_and_screenshots(self):
        page = FakePage(response=FakeResponse({"code": -2, "message": "没有权限"}))
        playwright_cm, _ = self._fake_playwright(lambda: page)
        cookie = "gld:sess=" + "A" * 180 + "; gld:sess.sig=" + "B" * 120
        with self._patch_playwright(playwright_cm), \
                patch.object(browser_checkin, "read_account") as read_account:
            code, result = browser_checkin.run(cookie_header=cookie, headless=True,
                                               apply_jitter=False, max_attempts=1)
        self.assertEqual(code, browser_checkin.EXIT_FATAL)
        self.assertFalse(result.ok)
        read_account.assert_not_called()
        self.screenshot_mock.assert_called_once()

    def test_jitter_is_skipped_in_dry_run(self):
        page = FakePage()
        playwright_cm, _ = self._fake_playwright(lambda: page)
        cookie = "gld:sess=" + "A" * 180 + "; gld:sess.sig=" + "B" * 120
        with self._patch_playwright(playwright_cm), \
                patch.object(browser_checkin, "sleep_with_jitter") as jitter, \
                patch.dict("os.environ", {"GLADOS_JITTER_MAX_MINUTES": "90"}):
            code, result = browser_checkin.run(cookie_header=cookie, headless=True,
                                               dry_run=True)
        jitter.assert_not_called()
        self.assertTrue(result.ok)

    def test_manual_run_skips_jitter(self):
        page = FakePage()
        playwright_cm, _ = self._fake_playwright(lambda: page)
        cookie = "gld:sess=" + "A" * 180 + "; gld:sess.sig=" + "B" * 120
        with self._patch_playwright(playwright_cm), \
                patch.object(browser_checkin, "sleep_with_jitter") as jitter, \
                patch.dict("os.environ", {"GH_EVENT": "workflow_dispatch"}):
            code, result = browser_checkin.run(cookie_header=cookie, headless=True,
                                               retry_delay=0)
        jitter.assert_not_called()
        self.assertTrue(result.ok)

    def test_scheduled_run_still_waits(self):
        page = FakePage()
        playwright_cm, _ = self._fake_playwright(lambda: page)
        cookie = "gld:sess=" + "A" * 180 + "; gld:sess.sig=" + "B" * 120
        with self._patch_playwright(playwright_cm), \
                patch.object(browser_checkin, "sleep_with_jitter") as jitter, \
                patch.dict("os.environ", {"GH_EVENT": "schedule",
                                          "GLADOS_JITTER_MIN_MINUTES": "5",
                                          "GLADOS_JITTER_MAX_MINUTES": "90"}):
            browser_checkin.run(cookie_header=cookie, headless=True, retry_delay=0)
        jitter.assert_called_once_with(5, 90)

    def test_missing_playwright_is_a_fatal_configuration_error(self):
        cookie = "gld:sess=" + "A" * 180 + "; gld:sess.sig=" + "B" * 120
        with patch.object(browser_checkin, "_load_sync_playwright",
                          side_effect=browser_checkin.FatalError("没有安装 playwright")), \
                self.assertRaises(browser_checkin.FatalError):
            browser_checkin.run(cookie_header=cookie, apply_jitter=False)


if __name__ == "__main__":
    unittest.main()

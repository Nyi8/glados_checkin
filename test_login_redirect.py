"""真实浏览器回归：全部请求在本地拦截，不申请验证码或访问真实账号。"""
import json
import os
import unittest
from urllib.parse import urlsplit

import email_login


@unittest.skipUnless(os.getenv("GLADOS_BROWSER_TESTS") == "1", "需要显式启用隔离浏览器测试")
class LoginRedirectBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls.runtime = sync_playwright().start()
        try:
            cls.browser = cls.runtime.chromium.launch(
                headless=True, channel=os.getenv("GLADOS_BROWSER_CHANNEL") or None, timeout=15_000)
        except Exception:
            cls.runtime.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.runtime.stop()

    def verify_redirect(self, authenticated):
        context = self.browser.new_context()
        session_reads = []

        def serve(route):
            path = urlsplit(route.request.url).path
            if path == "/login":
                return route.fulfill(content_type="text/html", body="""
                    <button id="login" onclick="fetch('/api/login', {method:'POST'})
                      .then(r=>r.json()).then(()=>location.href='/console')">Login</button>
                """)
            if path == "/console":
                return route.fulfill(content_type="text/html", body="<p>Console fixture</p>")
            if path == "/api/login":
                return route.fulfill(content_type="application/json", body='{"code":0}',
                                     headers={"Set-Cookie": "fixture_session=ok; Path=/; Secure; HttpOnly"})
            if path == "/api/user/session":
                headers = route.request.all_headers()
                session_reads.append((headers.get("authorization"), headers.get("cookie", "")))
                return route.fulfill(content_type="application/json",
                                     body=json.dumps({"code": 0 if authenticated else -2}))
            return route.abort()

        context.route("**/*", serve)
        try:
            page = context.new_page()
            page.goto(email_login.ORIGIN + "/login")
            page.evaluate("localStorage.setItem('glados.fp.v1', 'fp1.' + 'a'.repeat(48))")
            with page.expect_response(lambda r: email_login._is_response(r, "/api/login")) as reply:
                page.locator("#login").click()
            page.wait_for_url(email_login.CONSOLE_URL, wait_until="domcontentloaded")
            response = reply.value

            class LostBody:
                status, headers = response.status, response.headers

                def json(self):
                    # 确保即使浏览器以后保留了旧响应，仍覆盖响应体丢失分支。
                    raise RuntimeError("Response body unavailable after redirect")

            diagnostics = email_login._LoginDiagnostics(page)
            if authenticated:
                email_login._finish_login(page, LostBody(), diagnostics)
            else:
                with self.assertRaises(email_login.RetryableError):
                    email_login._finish_login(page, LostBody(), diagnostics)
            self.assertEqual(len(session_reads), 1)
            self.assertEqual(session_reads[0][0], "fp1." + "a" * 48)
            self.assertIn("fixture_session=ok", session_reads[0][1])
        finally:
            context.close()

    def test_redirect_with_unreadable_body_and_valid_session(self):
        self.verify_redirect(True)

    def test_console_without_valid_session_is_rejected(self):
        self.verify_redirect(False)


if __name__ == "__main__":
    unittest.main()

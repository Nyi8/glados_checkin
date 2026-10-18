import json
import unittest
from unittest.mock import MagicMock, Mock, patch

import api_checkin
import checkin
import diag_status
from glados_cookie import FatalError

COOKIE = "koa:sess=" + "A" * 60 + "; koa:sess.sig=" + "B" * 40


def response(payload=None, status=200):
    item = Mock(status_code=status)
    item.json.return_value = {"code": 0, "message": "Checkin! Got 9 Points"} if payload is None else payload
    return item


class NetworkError(Exception):
    pass


class APITests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.logs = patch.object(api_checkin, "log")
        self.log = self.logs.start()
        self.addCleanup(self.logs.stop)
        self.session = MagicMock()
        self.session.__enter__.return_value = self.session
        self.session.headers = {}
        self.session.request.side_effect = [response(), response({"code": 0, "data": {"leftDays": "100"}}),
                                            response({"points": "124.0000"})]
        self.requests = Mock(RequestException=NetworkError)
        self.requests.Session.return_value = self.session
        self.loader = patch.object(api_checkin, "_load_requests", return_value=self.requests)
        self.loader.start()
        self.addCleanup(self.loader.stop)

    def test_cookie_api_sends_exact_header_and_current_token_without_browser(self):
        with patch.object(checkin.browser_checkin, "run") as browser, patch.object(api_checkin.time, "sleep") as sleep:
            code, result = api_checkin.run(COOKIE)
        self.assertEqual(code, 0)
        self.assertTrue(result.ok)
        browser.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(self.session.headers["Cookie"], COOKIE)
        self.assertNotIn("Authorization", self.session.headers)
        first = self.session.request.call_args_list[0]
        self.assertEqual(first.args, ("post", "https://glados.cloud/api/user/checkin"))
        self.assertEqual(first.kwargs["json"], {"token": "glados.cloud"})
        self.assertFalse(first.kwargs["allow_redirects"])
        output = str(self.log.call_args_list)
        self.assertNotIn("A" * 60, output)
        self.assertIn("points=124.0000", output)

    def test_optional_original_device_values_are_used_only_when_configured(self):
        fp = "fp1." + "a" * 48
        with patch.dict("os.environ", {"GLADOS_FP": fp, "GLADOS_USER_AGENT": "original-browser"}):
            api_checkin.run(COOKIE)
        self.assertEqual(self.session.headers["Authorization"], fp)
        self.assertEqual(self.session.headers["User-Agent"], "original-browser")

    def test_dry_run_only_checks_status_with_cookie(self):
        self.session.request.side_effect = [response({"code": 0, "data": {"leftDays": "100"}})]
        code, _ = api_checkin.run(COOKIE, dry_run=True)
        self.assertEqual(code, 0)
        self.session.request.assert_called_once()
        self.assertEqual(self.session.request.call_args.args, ("get", "https://glados.cloud/api/user/status"))
        self.assertEqual(self.session.headers["Cookie"], COOKIE)

    def test_auth_device_and_captcha_failures_are_not_retried(self):
        for data, marker in (({"code": -2, "message": "没有权限"}, "更新完整"),
                             ({"code": 4, "reason": "device-mismatch"}, "device-mismatch"),
                             ({"code": 0, "captcha_required": True}, "人机验证")):
            self.session.request.reset_mock()
            self.session.request.side_effect = [response(data)]
            with patch.object(api_checkin.time, "sleep") as sleep:
                code, result = api_checkin.run(COOKIE)
            self.assertEqual(code, 1)
            self.assertIn(marker, result.message)
            self.session.request.assert_called_once()
            sleep.assert_not_called()

    def test_temporary_http_and_network_errors_retry_once(self):
        for error in (response(status=503), NetworkError("private-cookie")):
            self.session.request.reset_mock()
            self.session.request.side_effect = [error, response(), response({"code": 0}), response({"points": 9})]
            with patch.object(api_checkin.time, "sleep") as sleep:
                code, _ = api_checkin.run(COOKIE)
            self.assertEqual(code, 0)
            self.assertEqual(sum(call.args[0] == "post" for call in self.session.request.call_args_list), 2)
            sleep.assert_called_once_with(5)

    def test_temporary_failure_exit_code_two(self):
        self.session.request.side_effect = [response(status=429), response(status=503)]
        with patch.object(api_checkin.time, "sleep"):
            code, result = api_checkin.run(COOKIE)
        self.assertEqual(code, 2)
        self.assertEqual(result.status, "retryable")

    def test_redirect_and_html_are_never_treated_as_success(self):
        for status in (302, 403):
            self.session.request.reset_mock()
            self.session.request.side_effect = [response(status=status)]
            with self.assertRaises(FatalError):
                api_checkin.run(COOKIE)
            self.session.request.assert_called_once()
        html = response()
        html.json.side_effect = ValueError("private response")
        self.session.request.side_effect = [html]
        with self.assertRaises(FatalError) as caught:
            api_checkin.run(COOKIE)
        self.assertNotIn("private response", str(caught.exception))

    def test_repeat_success_and_unknown_payload_are_distinguished(self):
        self.assertTrue(api_checkin.classify_checkin({"code": 1, "message": "Checkin Repeats! Please Try Tomorrow"}).ok)
        self.assertEqual(api_checkin.classify_checkin({"code": 1, "message": "Unknown business error"}).status, "fatal")
        self.assertEqual(api_checkin.classify_checkin({"code": 1, "message": "please checkin via https://glados.cloud"}).status, "fatal")

    def test_account_display_failure_does_not_undo_confirmed_checkin(self):
        self.session.request.side_effect = [response(), response(status=503), response(status=403)]
        code, _ = api_checkin.run(COOKIE)
        self.assertEqual(code, 0)

    def test_legacy_token_is_updated(self):
        with patch.dict("os.environ", {"GLADOS_CHECKIN_TOKEN": "glados.one"}):
            api_checkin.run(COOKIE)
        self.assertEqual(self.session.request.call_args_list[0].kwargs["json"], {"token": "glados.cloud"})

    def test_backend_selection_and_main_honor_cookie_api(self):
        self.assertEqual(api_checkin.resolve_backend(), "api")
        with patch.dict("os.environ", {"GLADOS_AUTH_MODE": "email"}):
            self.assertEqual(api_checkin.resolve_backend(), "browser")
        with patch.dict("os.environ", {"GLADOS_CHECKIN_METHOD": "browser"}):
            self.assertEqual(api_checkin.resolve_backend(), "browser")
        with patch.dict("os.environ", {"GLADOS_CHECKIN_METHOD": "invalid"}), self.assertRaises(FatalError):
            api_checkin.resolve_backend()
        with patch.dict("os.environ", {"GLADOS_COOKIE": COOKIE}), patch.object(checkin.browser_checkin, "run") as browser:
            self.assertEqual(checkin.main([]), 0)
        browser.assert_not_called()

    def test_browser_mode_is_still_available_explicitly(self):
        with patch.dict("os.environ", {"GLADOS_COOKIE": COOKIE, "GLADOS_CHECKIN_METHOD": "browser"}), \
                patch.object(checkin.browser_checkin, "run", return_value=(0, api_checkin.CheckinResult("success"))) as browser:
            self.assertEqual(checkin.main([]), 0)
        browser.assert_called_once()
        self.session.request.assert_not_called()


class DiagnosticTests(unittest.TestCase):
    def test_status_probe_really_sends_cookie_regression(self):
        reply = MagicMock(status=200)
        reply.__enter__.return_value = reply
        reply.read.return_value = json.dumps({"code": 0, "data": {"leftDays": "100"}}).encode()
        with patch.object(diag_status, "require_cookie", return_value=COOKIE), \
                patch.object(diag_status.urllib.request, "urlopen", return_value=reply) as open_url, \
                patch.object(diag_status, "log"):
            self.assertEqual(diag_status.main(), 0)
        self.assertEqual(open_url.call_args.args[0].get_header("Cookie"), COOKIE)


if __name__ == "__main__":
    unittest.main()

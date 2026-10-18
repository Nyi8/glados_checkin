import unittest
from unittest.mock import Mock, patch

import browser_checkin
import api_checkin
import checkin

VALID_COOKIE = "gld:sess=" + "A" * 180 + "; gld:sess.sig=" + "B" * 120


def clean_environment(test_case):
    environment = patch.dict("os.environ", {}, clear=True)
    environment.start()
    test_case.addCleanup(environment.stop)
    logging = patch.object(checkin, "log")
    logging.start()
    test_case.addCleanup(logging.stop)


class MainTests(unittest.TestCase):
    def setUp(self):
        clean_environment(self)

    def test_success_returns_zero_without_email(self):
        with patch.dict("os.environ", {"GLADOS_COOKIE": VALID_COOKIE}), \
                patch.object(api_checkin, "run",
                             return_value=(checkin.EXIT_OK,
                                           browser_checkin.CheckinResult("success", "Checkin! Got 12"))) as run, \
                patch.object(checkin, "send_failure_email") as email:
            self.assertEqual(checkin.main([]), checkin.EXIT_OK)
        email.assert_not_called()
        run.assert_called_once()

    def test_fatal_result_returns_one_and_emails(self):
        with patch.dict("os.environ", {"GLADOS_COOKIE": VALID_COOKIE}), \
                patch.object(api_checkin, "run",
                             return_value=(checkin.EXIT_FATAL,
                                           browser_checkin.CheckinResult("fatal", "没有权限"))), \
                patch.object(checkin, "send_failure_email") as email:
            self.assertEqual(checkin.main([]), checkin.EXIT_FATAL)
        email.assert_called_once()

    def test_retryable_result_returns_two(self):
        with patch.dict("os.environ", {"GLADOS_COOKIE": VALID_COOKIE}), \
                patch.object(api_checkin, "run",
                             return_value=(checkin.EXIT_RETRYABLE,
                                           browser_checkin.CheckinResult("retryable", "timeout"))), \
                patch.object(checkin, "send_failure_email") as email:
            self.assertEqual(checkin.main([]), checkin.EXIT_RETRYABLE)
        email.assert_called_once()

    def test_bad_cookie_fails_before_starting_a_browser(self):
        with patch.dict("os.environ", {"GLADOS_COOKIE": "koa:sess=abc; koa:sess.sig=def"}), \
                patch.object(api_checkin, "run") as run, \
                patch.object(checkin, "send_failure_email") as email:
            self.assertEqual(checkin.main([]), checkin.EXIT_FATAL)
        run.assert_not_called()
        email.assert_called_once()

    def test_unexpected_exception_is_reported_as_fatal(self):
        with patch.dict("os.environ", {"GLADOS_COOKIE": VALID_COOKIE}), \
                patch.object(api_checkin, "run", side_effect=RuntimeError("boom")), \
                patch.object(checkin, "send_failure_email") as email:
            self.assertEqual(checkin.main([]), checkin.EXIT_FATAL)
        email.assert_called_once()


class FailureEmailTests(unittest.TestCase):
    def setUp(self):
        clean_environment(self)

    def test_missing_smtp_configuration_skips_email(self):
        with patch.object(checkin.smtplib, "SMTP") as smtp:
            checkin.send_failure_email("failed")
        smtp.assert_not_called()

    def test_configured_smtp_sends_redacted_body(self):
        environment = {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "user",
            "SMTP_PASSWORD": "password",
            "MAIL_TO": "someone@example.com",
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "123",
        }
        server = Mock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        with patch.dict("os.environ", environment), \
                patch.object(checkin.smtplib, "SMTP", return_value=server) as smtp:
            checkin.send_failure_email("session " + VALID_COOKIE)
        smtp.assert_called_once()
        server.login.assert_called_once()
        body = server.send_message.call_args.args[0].get_content()
        self.assertNotIn("A" * 180, body)
        self.assertIn("actions/runs/123", body)


if __name__ == "__main__":
    unittest.main()

import re
import time
import unittest
from email.message import EmailMessage
from unittest.mock import Mock, patch

import browser_checkin
import checkin
import email_login
from glados_cookie import FatalError, RetryableError, redact
from test_browser_checkin import FakePage, FakeResponse, FakeContextManager
import test_browser_checkin as browser_fakes


ENV = {"GLADOS_AUTH_MODE": "email", "GLADOS_EMAIL": "123@qq.com",
       "QQ_IMAP_AUTH_CODE": "test-mail-app-password", "GLADOS_MAIL_FROM": "login@example.com"}


def mail(body="Your access code: 482731", sender="login@example.com", recipient="123@qq.com",
         html=False):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = "GLaDOS Access Code"
    message.set_content(body, subtype="html" if html else "plain")
    return message.as_bytes()


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict("os.environ", ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.logs = patch.object(email_login, "log")
        self.logs.start()
        self.addCleanup(self.logs.stop)

    def extract(self, raw, pattern=r"\b[0-9]{6}\b"):
        return email_login.extract_code(raw, ENV["GLADOS_MAIL_FROM"], ENV["GLADOS_EMAIL"],
                                        re.compile(pattern))

    def test_mime_html_and_redaction(self):
        self.assertEqual(self.extract(mail("<p>Code: <b>482731</b></p>", html=True)), "482731")
        self.assertNotIn("482731", redact("Code 482731"))

    def test_wrong_sender_recipient_and_ambiguous_codes_are_ignored(self):
        for raw in (mail(sender="other@example.com"), mail(recipient="other@qq.com"),
                    mail("482731 729381"), mail("No access code yet")):
            self.assertIsNone(self.extract(raw))

    def test_duplicate_mime_codes_are_not_ambiguous(self):
        self.assertEqual(self.extract(mail("482731 and 482731")), "482731")

    def test_custom_pattern_and_attachment_exclusion(self):
        self.assertEqual(self.extract(mail("Access Code: Ab12Cd"), r"Access Code:\s*([A-Za-z0-9]{6})"),
                         "Ab12Cd")
        message = EmailMessage()
        message["From"], message["To"] = ENV["GLADOS_MAIL_FROM"], ENV["GLADOS_EMAIL"]
        message.set_content("Code 482731")
        message.add_attachment(b"Code 729381", maintype="text", subtype="plain", filename="data.txt")
        self.assertEqual(self.extract(message.as_bytes()), "482731")

    def test_configuration_rejects_bad_regex_and_missing_secrets(self):
        for values in ({"GLADOS_CODE_REGEX": "("}, {"QQ_IMAP_AUTH_CODE": ""},
                       {"GLADOS_MAIL_FROM": ""}, {"GLADOS_MAIL_TIMEOUT_SECONDS": "900"}):
            with patch.dict("os.environ", values), self.assertRaises(FatalError):
                email_login.QQMailbox()

    def test_mailbox_reads_only_new_messages_with_peek(self):
        client = Mock()
        client.select.return_value = ("OK", [b"1"])
        client.response.return_value = ("UIDVALIDITY", [b"7"])
        recent = time.strftime('%d-%b-%Y %H:%M:%S +0000', time.gmtime())
        old = '01-Jan-2020 00:00:00 +0000'
        def uid(command, *args):
            if command == "search":
                return "OK", [b"1" if not getattr(client, "baseline_done", False) else b"1 2 3 4"]
            headers = ('1 (INTERNALDATE "%s" BODY[] {100}' %
                       (old if args[0] == b"4" else recent)).encode()
            return "OK", [(headers, mail(sender="other@example.com") if args[0] == b"3" else mail())]
        client.baseline_done = False
        client.uid.side_effect = uid
        with patch.object(email_login.imaplib, "IMAP4_SSL", return_value=client):
            with email_login.QQMailbox() as mailbox:
                client.baseline_done = True
                self.assertEqual(mailbox.wait_for_code(time.time()), "482731")
        client.select.assert_called_with("INBOX", readonly=True)
        fetched = [c.args for c in client.uid.call_args_list if c.args[0] == "fetch"]
        self.assertEqual([c[1] for c in fetched], [b"4", b"3", b"2"])
        self.assertTrue(all(c[2] == "(INTERNALDATE BODY.PEEK[])" for c in fetched))
        client.logout.assert_called_once()

    def test_mailbox_timeout_and_uidvalidity_change(self):
        mailbox = email_login.QQMailbox()
        mailbox.client = Mock()
        mailbox.client.select.return_value = ("OK", [b"0"])
        mailbox.client.response.return_value = ("UIDVALIDITY", [b"changed"])
        with self.assertRaises(RetryableError):
            mailbox.wait_for_code(time.time())
        with patch.object(email_login.time, "monotonic", side_effect=[0, 999]), \
                self.assertRaises(RetryableError):
            mailbox.wait_for_code(time.time())

    def test_reply_stops_on_captcha_and_throttling(self):
        for response, error in ((FakeResponse({"captcha_required": True}), FatalError),
                                (FakeResponse(status=429), RetryableError),
                                (FakeResponse({"code": -1}), FatalError)):
            with self.assertRaises(error):
                email_login._check_reply(response)

    def test_response_match_is_exact_origin_and_post(self):
        response = FakeResponse()
        for url, method, valid in (("https://glados.cloud/api/login", "POST", True),
                                   ("https://other.example/api/login", "POST", False),
                                   ("https://glados.cloud/api/login", "GET", False)):
            response.url, response.request.method = url, method
            self.assertEqual(email_login._is_response(response, "/api/login"), valid)

    def test_login_uses_verified_page_forms(self):
        page = Mock()
        page.expect_response.return_value = FakeContextManager(FakeResponse({"code": 0}))
        mailbox = Mock(address=ENV["GLADOS_EMAIL"])
        mailbox.wait_for_code.return_value = "482731"
        with patch.object(email_login, "QQMailbox") as box:
            box.return_value.__enter__.return_value = mailbox
            email_login.login(page)
        page.locator.assert_any_call("#email")
        page.locator.assert_any_call("#mailcode")
        self.assertEqual(page.expect_response.call_count, 2)
        page.wait_for_url.assert_called_once()

    def test_no_mail_resend_and_no_sensitive_playwright_exception(self):
        page = Mock()
        page.locator.return_value.fill.side_effect = RuntimeError("secret-code-482731")
        with patch.object(email_login, "QQMailbox") as box, self.assertRaises(RetryableError) as error:
            email_login.login(page)
        self.assertNotIn("482731", str(error.exception))
        box.return_value.__enter__.return_value.wait_for_code.assert_not_called()

    def test_main_needs_no_external_cookie(self):
        with patch.object(browser_checkin, "run", return_value=(0, browser_checkin.CheckinResult("success"))), \
                patch.object(checkin, "require_cookie") as cookie:
            self.assertEqual(checkin.main([]), 0)
        cookie.assert_not_called()


class EmailBrowserTests(unittest.TestCase):
    def test_same_context_no_external_credentials_and_no_login_logging(self):
        page = FakePage()
        cm, context = browser_fakes.RunTests()._fake_playwright(lambda: page)
        def login(current):
            self.assertIs(current, page)
            self.assertFalse(hasattr(page, "handlers"))
        with patch.dict("os.environ", dict(ENV, GLADOS_FP="invalid-ignored", GLADOS_USER_AGENT="ignored"), clear=True), \
                patch.object(browser_checkin, "_load_sync_playwright", return_value=lambda: cm), \
                patch.object(browser_checkin, "login_with_email", side_effect=login) as signin, \
                patch.object(browser_checkin, "read_account"), \
                patch.object(browser_checkin, "_checkin_with_retry", return_value=browser_checkin.CheckinResult("success")) as check:
            code, _ = browser_checkin.run(apply_jitter=False)
        self.assertEqual(code, 0)
        signin.assert_called_once_with(page)
        check.assert_called_once_with(page, browser_checkin.MAX_ATTEMPTS, browser_checkin.RETRY_DELAY_SECONDS)
        context.add_cookies.assert_not_called()
        context.add_init_script.assert_not_called()
        context.close.assert_called_once()

    def test_login_failure_closes_context_without_screenshot(self):
        page = FakePage()
        cm, context = browser_fakes.RunTests()._fake_playwright(lambda: page)
        with patch.dict("os.environ", ENV, clear=True), \
                patch.object(browser_checkin, "_load_sync_playwright", return_value=lambda: cm), \
                patch.object(browser_checkin, "login_with_email", side_effect=FatalError("captcha")), \
                patch.object(browser_checkin, "_screenshot") as screenshot, \
                self.assertRaises(FatalError):
            browser_checkin.run(apply_jitter=False)
        context.close.assert_called_once()
        screenshot.assert_not_called()

    def test_dry_run_does_not_send_email(self):
        with patch.dict("os.environ", ENV, clear=True), \
                patch.object(browser_checkin, "_load_sync_playwright"), \
                patch.object(browser_checkin, "login_with_email") as login, \
                self.assertRaises(FatalError):
            browser_checkin.run(dry_run=True)
        login.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import glados_cookie as cookie_util


def header(*pairs):
    return "; ".join(f"{name}={value}" for name, value in pairs)


# 真实 gld 会话两项都很长，用足够长的占位值让长度校验也能覆盖。
GLD_SESS = "A" * 180 + "+/=="
GLD_SIG = "B" * 120 + "-_"
KOA_SESS = "C" * 60
KOA_SIG = "D" * 40


class NormalizeCookieTests(unittest.TestCase):
    def test_signed_header_is_preserved_byte_for_byte(self):
        cookie = "extra=a&amp%2F;; koa:sess=abc+/==; koa:sess.sig=sig-_; device=current"
        self.assertEqual(cookie_util.normalize_cookie(cookie), cookie)

    def test_raw_chat_values_with_html_space_and_padding(self):
        self.assertEqual(
            cookie_util.normalize_cookie("“abc+/== &#x20;\n sig-_  ”"),
            "koa:sess=abc+/==; koa:sess.sig=sig-_",
        )

    def test_prefix_and_quotes_are_removed_without_changing_values(self):
        self.assertEqual(
            cookie_util.normalize_cookie('"Cookie: koa:sess=value; koa:sess.sig=signature"'),
            "koa:sess=value; koa:sess.sig=signature",
        )

    def test_cookie_editor_export_retains_all_values(self):
        exported = [
            {"name": "koa:sess", "value": "abc+/==", "domain": ".glados.cloud"},
            {"name": "gld:sess", "value": "sess+/==", "domain": "glados.cloud"},
            {"name": "gld:sess.sig", "value": "signature", "domain": "glados.cloud"},
            {"name": "device", "value": "a&amp", "domain": "glados.cloud"},
        ]
        self.assertEqual(
            cookie_util.normalize_cookie(json.dumps(exported)),
            "koa:sess=abc+/==; gld:sess=sess+/==; gld:sess.sig=signature; device=a&amp",
        )

    def test_rejects_ambiguous_or_injected_input(self):
        for value in ("", "not a valid cookie", "koa:sess=value;\r\nX-Header=injected"):
            with self.subTest(value=value), self.assertRaises(cookie_util.FatalError):
                cookie_util.normalize_cookie(value)


class SessionKindTests(unittest.TestCase):
    def test_current_session_is_detected(self):
        cookie = header(("gld:sess", GLD_SESS), ("gld:sess.sig", GLD_SIG))
        self.assertEqual(cookie_util.session_cookie_kind(cookie), "gld")
        self.assertEqual(cookie_util.require_current_session(cookie), "gld")

    def test_legacy_session_is_reported_with_actionable_message(self):
        cookie = header(("koa:sess", KOA_SESS), ("koa:sess.sig", KOA_SIG))
        self.assertEqual(cookie_util.session_cookie_kind(cookie), "koa")
        with self.assertRaises(cookie_util.FatalError) as raised:
            cookie_util.require_current_session(cookie)
        self.assertIn("gld:sess", str(raised.exception))

    def test_real_world_short_cookie_passes(self):
        # 实测真实值就这么短：gld 两项整串只有 105 字节，必须放行。
        cookie = header(("gld:sess", "gld_f6038a8db8f6c2e63a47eee77f4f181a46fe9515b832bf02"),
                        ("gld:sess.sig", "CdjjYfECRG_YACRl4rfE8a4-sHlZs"))
        self.assertLess(len(cookie), 200)
        self.assertEqual(cookie_util.require_current_session(cookie), "gld")

    def test_real_world_cookie_with_koa_pair_passes(self):
        cookie = header(("gld:sess", "gld_f6038a8db8f6c2e63a47eee77f4f181a46fe9515b832bf02"),
                        ("gld:sess.sig", "CdjjYfECRG_YACRl4rfE8a4-sHlZs"),
                        ("koa:sess", "eyJ1c2VySWQiOjU1MzEzMSwiX2V4cCI6MTc5NTg2"),
                        ("koa:sess.sig", "ktimoHOcIRhw2lO2fpNGaNSlzol"))
        self.assertLess(len(cookie), 250)
        self.assertEqual(cookie_util.require_current_session(cookie), "gld")

    def test_truncated_value_is_rejected(self):
        # 线上出现过 103 字节的输入：字段名齐了，但值被截断。
        cookie = header(("gld:sess", "gl"), ("gld:sess.sig", "Cd"))
        with self.assertRaises(cookie_util.FatalError) as raised:
            cookie_util.require_current_session(cookie)
        self.assertIn("截断", str(raised.exception))

    def test_incomplete_current_session_is_rejected_by_name(self):
        cookie = header(("gld:sess", GLD_SESS), ("koa:sess", KOA_SESS),
                        ("koa:sess.sig", KOA_SIG), ("extra", "x" * 200))
        with self.assertRaises(cookie_util.FatalError) as raised:
            cookie_util.require_current_session(cookie)
        self.assertIn("gld:sess.sig", str(raised.exception))

    def test_parse_cookie_pairs_keeps_every_field(self):
        cookie = header(("gld:sess", GLD_SESS), ("gld:sess.sig", GLD_SIG), ("device", "abc"))
        self.assertEqual(
            cookie_util.parse_cookie_pairs(cookie),
            [("gld:sess", GLD_SESS), ("gld:sess.sig", GLD_SIG), ("device", "abc")],
        )


class RequireCookieTests(unittest.TestCase):
    def setUp(self):
        self.secrets = patch.object(cookie_util, "SECRET_VALUES", set())
        self.secrets.start()
        self.addCleanup(self.secrets.stop)

    def test_require_cookie_logs_fingerprint_without_values(self):
        secret = header(("gld:sess", GLD_SESS), ("gld:sess.sig", GLD_SIG))
        output = io.StringIO()
        with patch.dict("os.environ", {"GLADOS_COOKIE": secret}), redirect_stdout(output):
            self.assertEqual(cookie_util.require_cookie(), secret)
            cookie_util.log("echo " + secret)
        text = output.getvalue()
        self.assertNotIn(GLD_SESS, text)
        self.assertNotIn(GLD_SIG, text)
        self.assertIn("指纹", text)
        self.assertIn("会话类型=gld", text)

    def test_require_cookie_rejects_missing_new_session(self):
        with patch.dict("os.environ", {"GLADOS_COOKIE": ""}), \
                self.assertRaises(cookie_util.FatalError):
            cookie_util.require_cookie()

    def test_complete_koa_cookie_is_allowed_and_validity_is_left_to_server(self):
        cookie = header(("koa:sess", KOA_SESS), ("koa:sess.sig", KOA_SIG))
        with patch.dict("os.environ", {"GLADOS_COOKIE": cookie}):
            self.assertEqual(cookie_util.require_cookie(), cookie)

    def test_partial_and_truncated_sessions_are_rejected(self):
        for cookie in ("koa:sess=" + KOA_SESS, "koa:sess=abc; koa:sess.sig=def"):
            with patch.dict("os.environ", {"GLADOS_COOKIE": cookie}), self.assertRaises(cookie_util.FatalError):
                cookie_util.require_cookie()


if __name__ == "__main__":
    unittest.main()

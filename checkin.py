"""GLaDOS 每日签到编排入口。

Cookie 默认由 api_checkin.py 请求接口；邮箱登录与浏览器点击备选由 browser_checkin.py 完成。
本模块负责选择后端、失败通知与退出码。

退出码：0 成功或今日已签到，1 配置/认证等需要人工处理的错误，2 临时网络或服务故障。
"""
import smtplib
import sys
from email.message import EmailMessage

import browser_checkin
import api_checkin
from email_login import email_mode
from glados_cookie import (
    FatalError,
    RetryableError,
    get_env,
    log,
    redact,
    require_cookie,
)

EXIT_OK, EXIT_FATAL, EXIT_RETRYABLE = 0, 1, 2


def send_failure_email(summary):
    host, username = get_env("SMTP_HOST"), get_env("SMTP_USERNAME")
    password, recipient = get_env("SMTP_PASSWORD"), get_env("MAIL_TO")
    if not all((host, username, password, recipient)):
        log("未配置完整 SMTP，跳过失败邮件。")
        return
    try:
        port = int(get_env("SMTP_PORT", "587"))
        use_ssl = get_env("SMTP_USE_SSL", "false").lower() in ("1", "true", "yes", "on")
        message = EmailMessage()
        message["Subject"] = "[GLaDOS] Automatic checkin failed"
        message["From"], message["To"] = get_env("MAIL_FROM", username), recipient
        body = f"GLaDOS checkin failed.\n\n{redact(summary)}\n"
        server_url = get_env("GITHUB_SERVER_URL", "https://github.com")
        repository, run_id = get_env("GITHUB_REPOSITORY"), get_env("GITHUB_RUN_ID")
        if repository and run_id:
            body += f"\nActions: {server_url}/{repository}/actions/runs/{run_id}\n"
        message.set_content(body)
        smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_class(host, port, timeout=30) as server:
            if not use_ssl:
                server.starttls()
            server.login(username, password)
            server.send_message(message)
        log("失败邮件已发送。")
    except (OSError, ValueError, smtplib.SMTPException):
        log("失败邮件发送失败，请检查 SMTP 配置。")


def main(argv=None):
    args = browser_checkin.parse_args(argv)
    browser_checkin.apply_arg_overrides(args)
    try:
        cookie = None if email_mode() else require_cookie()
        backend = api_checkin.resolve_backend()
        if backend == "api":
            code, result = api_checkin.run(cookie_header=cookie, dry_run=args.dry_run)
        else:
            code, result = browser_checkin.run(
                cookie_header=cookie,
                headless=not args.headed,
                dry_run=args.dry_run,
                apply_jitter=False,
                channel=args.channel,
            )
        if code != EXIT_OK:
            hint = browser_checkin._fatal_hint(result) if backend == "browser" else "请查看 Cookie API 的具体回复。"
            send_failure_email(f"{result.message}\n{hint}")
        return code
    except FatalError as exc:
        log(f"ERROR: {exc}")
        send_failure_email(str(exc))
        return EXIT_FATAL
    except RetryableError as exc:
        log(f"ERROR: {exc}")
        send_failure_email(str(exc))
        return EXIT_RETRYABLE
    except Exception as exc:  # 未知异常也要给出可读结论，避免 Actions 只有堆栈
        log(f"ERROR: 未预期的失败：{redact(exc)}")
        send_failure_email(str(exc))
        return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())

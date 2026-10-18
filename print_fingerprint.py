"""独立测试浏览器的设备指纹探测，不是现有登录浏览器的指纹导出工具。

本工具创建全新浏览器上下文，不能读取平时 Chrome 中已有的登录缓存。
实际配置请在已登录 GLaDOS 网页的控制台执行：
localStorage.getItem('glados.fp.v1')；User-Agent 用 navigator.userAgent 读取。

用法（本机、会打开一个浏览器窗口）：
    pwsh -File setup_env.ps1
    & .venv\\Scripts\\python.exe print_fingerprint.py            # 显示指纹
    & .venv\\Scripts\\python.exe print_fingerprint.py fp.txt      # 同时写入文件

输出值仅属于独立测试浏览器，不能与其他浏览器的 Cookie 混用。
"""
import argparse
import os
import sys
import time

from glados_cookie import FatalError, get_env, log

ORIGIN = "https://glados.cloud"
FP_KEY = "glados.fp.v1"


def _load_sync_playwright():
    from playwright.sync_api import sync_playwright

    return sync_playwright


def main(argv=None):
    parser = argparse.ArgumentParser(description="探测独立测试浏览器的 GLaDOS 设备指纹")
    parser.add_argument("output", nargs="?", default=None, help="可选：把指纹写入该文件")
    parser.add_argument("--channel", default=None, help="使用已安装的浏览器通道（chrome / msedge）")
    parser.add_argument("--timeout", type=int, default=60, help="等待站点写入指纹的秒数")
    args = parser.parse_args(argv)

    try:
        sync_playwright = _load_sync_playwright()
    except ImportError:
        log("没有安装 playwright：先执行 pwsh -File setup_env.ps1")
        return 1

    channel = get_env("GLADOS_BROWSER_CHANNEL") or args.channel or ""
    user_agent = get_env("GLADOS_USER_AGENT")
    log("注意：此工具创建新浏览器，不能读取原已登录浏览器的指纹。配置请使用原网页控制台。")
    log(f"打开 {ORIGIN}（独立测试浏览器，headless=False）")
    with sync_playwright() as playwright:
        launch_kwargs = {"headless": False, "args": ["--disable-blink-features=AutomationControlled"]}
        if channel:
            launch_kwargs["channel"] = channel
        browser = playwright.chromium.launch(**launch_kwargs)
        try:
            context_options = {"viewport": {"width": 1440, "height": 900}, "locale": "zh-CN"}
            if user_agent:
                context_options["user_agent"] = user_agent
            context = browser.new_context(**context_options)
            page = context.new_page()
            page.goto(ORIGIN, wait_until="domcontentloaded", timeout=60_000)
            fingerprint = ""
            deadline = time.time() + args.timeout
            while time.time() < deadline:
                try:
                    value = page.evaluate(
                        "(key) => window.localStorage.getItem(key)", FP_KEY)
                except Exception:
                    value = None
                if value:
                    fingerprint = str(value).strip()
                    break
                page.wait_for_timeout(1_000)
            context.close()
        finally:
            browser.close()

    if not fingerprint:
        log("没有读到指纹：请确认这个浏览器已经登录过 glados.cloud 并停留在页面几秒后重试。")
        return 1
    if not fingerprint.startswith("fp1."):
        log(f"读到的值不像指纹：{fingerprint[:20]}…")
        return 1

    log(f"设备指纹 = {fingerprint}")
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(fingerprint + "\n")
        log(f"已写入文件 {args.output}（可作为 GLADOS_FP_FILE）")
    log("以上仅为测试浏览器的探测值；不要与其他浏览器导出的 Cookie 混用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

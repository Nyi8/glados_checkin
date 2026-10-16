"""只做一次 /api/user/status 查询，判断 Cookie 是否还有效（不签到、不点击、不起浏览器）。

用法：
    $env:GLADOS_COOKIE='gld:sess=...; gld:sess.sig=...'
    python -u diag_status.py
"""
import json
import sys
import urllib.error
import urllib.request

from glados_cookie import FatalError, get_env, log, require_cookie

STATUS_URL = "https://glados.cloud/api/user/status"


def main():
    try:
        cookie = require_cookie()
    except FatalError as exc:
        log(f"ERROR: {exc}")
        return 1

    user_agent = get_env("GLADOS_USER_AGENT") or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )
    request = urllib.request.Request(STATUS_URL, headers={
        "Accept": "application/json, text/plain, */*",
        "User-Agent": user_agent,
        "Referer": "https://glados.cloud/console/checkin",
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        log(f"HTTP {exc.code}")
        return 1
    except Exception as exc:
        log(f"请求失败：{exc}")
        return 2

    log(f"HTTP {status}")
    try:
        payload = json.loads(raw)
    except ValueError:
        log("返回的不是 JSON，站点可能返回了验证页。")
        return 1

    code = payload.get("code")
    log(f"code={code}，message={payload.get('message', '<none>')}")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if isinstance(data, dict):
        log(f"leftDays={data.get('leftDays', '<none>')}，plan={data.get('vip', '<none>')}")
    if code == 0:
        log("结论：Cookie 有效，服务端认得这个会话。")
        return 0
    log("结论：服务端不认这个会话（Cookie 过期/被吊销/不完整），需要重新复制。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

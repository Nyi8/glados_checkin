"""生成下次签到时间，并原子提交 config + 工作流 cron；不等待、不触发签到。"""
import argparse
import base64
import json
import math
import os
import random
import re
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from glados_cookie import BEIJING_TZ, FatalError, SECRET_VALUES, get_env, log

CONFIG_PATH = "config/schedule.json"
WORKFLOW_PATH = ".github/workflows/checkin.yml"
BLOCK = re.compile(r"(^[ \t]*# BEGIN GLADOS RANDOM SCHEDULE\r?\n)(.*?)(^[ \t]*# END GLADOS RANDOM SCHEDULE)",
                   re.M | re.S)


def validate_config(config):
    if not isinstance(config, dict):
        raise FatalError("排程配置必须是 JSON 对象。")
    base = config.get("base_time")
    if not isinstance(base, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", base):
        raise FatalError("base_time 必须为北京时间 HH:MM。")
    low, high = config.get("min_offset_minutes"), config.get("max_offset_minutes")
    if type(low) is not int or type(high) is not int or not 0 <= low <= high <= 120:
        raise FatalError("排程偏移必须满足 0 <= min_offset_minutes <= max_offset_minutes <= 120。")
    hour, minute = map(int, base.split(":"))
    if hour * 60 + minute + high >= 1440:
        raise FatalError("随机时间窗口不能跨过北京时间午夜。")
    planned = config.get("next_run_at")
    offset = config.get("next_offset_minutes")
    if planned is None:
        if offset is not None or config.get("cron") != "0 1 * * *":
            raise FatalError("首次排程应保留 next_run_at/next_offset_minutes=null 和初始 cron。")
    else:
        if type(offset) is not int or not 0 <= offset <= 120:
            raise FatalError("next_offset_minutes 必须是 0–120 的整数。")
        try:
            target = datetime.fromisoformat(planned)
            if target.utcoffset() != timedelta(hours=8) or target.second or target.microsecond:
                raise ValueError()
        except (TypeError, ValueError):
            raise FatalError("next_run_at 必须是带 +08:00 时区、精确到分钟的时间。") from None
    return config


def cron_for(target):
    utc = target.astimezone(timezone.utc)
    # 指定下一次的日/月，避免将 cron 改为今天较晚时间后，当天又触发一次。
    return f"{utc.minute} {utc.hour} {utc.day} {utc.month} *"


def workflow_cron(source):
    matches = list(BLOCK.finditer(source))
    if len(matches) != 1:
        raise FatalError("工作流必须有且只有一个 GLADOS RANDOM SCHEDULE 标记块。")
    line = re.fullmatch(r'([ \t]+)- cron: "([^"\r\n]+)"\r?\n', matches[0][2])
    if not line:
        raise FatalError("随机排程标记块必须只包含一行 cron。")
    return line[2]


def replace_cron(source, cron):
    workflow_cron(source)  # 先校验，拒绝覆盖不认识的工作流结构。
    match = BLOCK.search(source)
    indent = re.match(r"[ \t]+", match[2])[0]
    newline = "\r\n" if "\r\n" in match[1] else "\n"
    return source[:match.start(2)] + f'{indent}- cron: "{cron}"{newline}' + source[match.end(2):]


def plan_next(config, source, now, repair=False, rng=None):
    """正常运行安排明天；保活仅修复过去日期或不同步的 cron。"""
    validate_config(config)
    current_cron = workflow_cron(source)
    now = now.astimezone(BEIJING_TZ)
    planned = datetime.fromisoformat(config["next_run_at"]) if config["next_run_at"] else None
    if repair and (planned is None or planned.date() >= now.date()):
        expected = cron_for(planned) if planned else config["cron"]
        if current_cron == expected and config["cron"] == expected:
            return None
        updated = dict(config, cron=expected)
        return updated, replace_cron(source, expected)

    hour, minute = map(int, config["base_time"].split(":"))
    today_base = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    low, high = config["min_offset_minutes"], config["max_offset_minutes"]
    target_base = today_base + timedelta(days=1)
    if repair:
        # 恢复遗漏的排程时，尽量安排在今天尚未过去的窗口，预留 5 分钟供 GitHub 更新。
        earliest = max(low, math.ceil((now + timedelta(minutes=5) - today_base).total_seconds() / 60))
        if earliest <= high:
            target_base, low = today_base, earliest
    offset = (rng or random.SystemRandom()).randint(low, high)
    target = target_base + timedelta(minutes=offset)
    cron = cron_for(target)
    updated = dict(config, next_offset_minutes=offset, next_run_at=target.isoformat(), cron=cron)
    return updated, replace_cron(source, cron)


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub API HTTP {status}")


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise FatalError("GITHUB_REPOSITORY 无效。")
        self.root = f"https://api.github.com/repos/{repository}"
        self.token = token
        SECRET_VALUES.add(token)

    def call(self, method, path, payload=None):
        request = Request(self.root + path,
                          data=json.dumps(payload).encode("utf-8") if payload is not None else None,
                          method=method, headers={"Authorization": f"Bearer {self.token}",
                                                  "Accept": "application/vnd.github+json",
                                                  "Content-Type": "application/json",
                                                  "User-Agent": "glados-random-schedule",
                                                  "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            raise APIError(exc.code) from None
        except (OSError, URLError, ValueError):
            raise FatalError("连接 GitHub API 失败；未确认排程更新，请查看仓库中的 config。") from None

    def text_at(self, path, head):
        item = self.call("GET", f"/contents/{quote(path, safe='/')}?ref={quote(head, safe='')}")
        if item.get("encoding") != "base64":
            raise FatalError("GitHub 配置文件未以预期的 base64 格式返回。")
        return base64.b64decode(item["content"]).decode("utf-8")


def update_remote(api, branch, now, repair=False):
    ref = "/git/refs/heads/" + quote(branch, safe="")
    for attempt in range(3):
        head = api.call("GET", "/git/ref/heads/" + quote(branch, safe=""))["object"]["sha"]
        config = json.loads(api.text_at(CONFIG_PATH, head))
        source = api.text_at(WORKFLOW_PATH, head)
        result = plan_next(config, source, now, repair)
        if result is None:
            log("随机排程仍有效，无需修改。")
            return None
        updated, workflow = result
        tree_sha = api.call("GET", "/git/commits/" + head)["tree"]["sha"]
        tree = api.call("POST", "/git/trees", {"base_tree": tree_sha, "tree": [
            {"path": CONFIG_PATH, "mode": "100644", "type": "blob",
             "content": json.dumps(updated, ensure_ascii=False, indent=2) + "\n"},
            {"path": WORKFLOW_PATH, "mode": "100644", "type": "blob", "content": workflow},
        ]})["sha"]
        commit = api.call("POST", "/git/commits", {
            "message": f"chore: schedule checkin for {updated['next_run_at'] or 'bootstrap'}",
            "tree": tree, "parents": [head],
        })["sha"]
        try:
            # 不强推；并发提交导致分叉时重新读取最新文件，不覆盖用户改动。
            api.call("PATCH", ref, {"sha": commit, "force": False})
        except APIError as exc:
            if exc.status in (409, 422) and attempt < 2:
                continue
            raise
        log(f"下一次签到：{updated['next_run_at']}（北京时间），"
            f"随机偏移={updated['next_offset_minutes']} 分钟，UTC cron={updated['cron']}。")
        return updated
    raise FatalError("默认分支连续发生并发更新，排程未完成。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="仅修复过期或不同步的排程（保活用）")
    parser.add_argument("--preview", action="store_true", help="只根据本地配置预览，不写文件或访问 GitHub")
    args = parser.parse_args(argv)
    try:
        now = datetime.now(BEIJING_TZ)
        if args.preview:
            with open(CONFIG_PATH, encoding="utf-8") as handle:
                config = json.load(handle)
            with open(WORKFLOW_PATH, encoding="utf-8") as handle:
                source = handle.read()
            result = plan_next(config, source, now, args.repair)
            print(json.dumps(result[0] if result else config, ensure_ascii=False, indent=2))
            return 0
        token = get_env("SCHEDULE_TOKEN")
        if not token:
            message = "未配置 SCHEDULE_TOKEN；需本仓库 Contents 和 Workflows 的读写权限，当前 cron 保持不变。"
            if args.repair:
                log(message)
                return 0
            raise FatalError(message)
        branch = get_env("DEFAULT_BRANCH")
        if not branch:
            raise FatalError("缺少 DEFAULT_BRANCH，拒绝猜测要更新的分支。")
        updated = update_remote(GitHub(get_env("GITHUB_REPOSITORY"), token), branch, now, args.repair)
        summary = get_env("GITHUB_STEP_SUMMARY")
        if summary and updated:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(f"\n下一次签到：**{updated['next_run_at']}**（北京时间）；"
                             f"随机偏移 {updated['next_offset_minutes']} 分钟；无需 runner 等待。\n")
        return 0
    except APIError as exc:
        log(f"ERROR: 排程更新失败（GitHub HTTP {exc.status}）；检查 SCHEDULE_TOKEN 的权限、有效期和默认分支保护。")
    except (FatalError, ValueError, KeyError, OSError) as exc:
        log(f"ERROR: 排程更新失败：{exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

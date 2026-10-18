"""抖音博主 → B站 自动同步入口。

用法：
    python douyin_sync.py <github_token> <gist_id> [--logLevel DEBUG] [--dry-run]
    python douyin_sync.py --dry-run          # 本地模式：不碰 Gist，只打印将同步的作品

流程：
    1. 从 Gist 读取抖音登录态 douyin_cookie_<账号>.json、B站登录态与同步状态
    2. 读博主页配置 douyin_channels.json（Gist 同名文件优先，回退仓库文件）
    3. 用 patchright 打开博主主页、拦截作品接口，过滤已同步/图文作品
    4. 下载无水印视频 + 封面，用 BilibiliUploader 投稿到 B站
    5. 每成功一条立即回写进度到 Gist douyin_synced.json

首次运行只记录基线（主页当前作品全部标记为已见），不推送历史作品；之后只同步新作品。
必须用有头浏览器：抖音对无头浏览器的反爬会让作品接口不触发，CI 用 xvfb-run 提供虚拟显示。
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time

import util
from bili_uploader import BilibiliUploader
from douyin_downloader import DouyinDownloader
from gist_store import GistStore

SYNCED_FILE = "douyin_synced.json"          # Gist 里的同步进度文件
CHANNELS_FILE = "douyin_channels.json"      # 博主页配置（Gist 同名文件优先）
DOUYIN_COOKIE_PREFIX = "douyin_cookie"      # Gist 文件名前缀：douyin_cookie_<账号>.json
BILI_COOKIE_FILE = "cookie.json"            # Gist 里默认 B站登录态文件名
DOUYIN_COOKIE_DIR = "douyin_cookies"        # 本地登录态落地目录
DEFAULT_DESC = "转载自抖音，喜欢的话求点赞投币关注！"
VERIFY = os.environ.get("verify", "1") == "1"
UPLOAD_SLEEP_SECOND = int(os.environ.get("DOUYIN_SYNC_SLEEP", "120"))
# 抖音文案里 util.clean 没覆盖到的表情/变体选择符，标题和标签再过滤一遍
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]")


def _clean_text(text):
    return _EMOJI_RE.sub("", util.clean(text or ""))


def _title_from_desc(desc):
    """抖音文案 → B站标题：取首个非空行、去掉 # 话题，最多 80 字。"""
    text = _clean_text(desc)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "抖音视频"
    title = lines[0].split("#", 1)[0].strip(" ·-—|｜~～")
    if not title:
        title = re.sub(r"\s+", " ", text.split("#", 1)[0]).strip()
    return (title or "抖音视频")[:80]


def _tags_from_desc(desc, extra=""):
    """抖音文案里的 #话题 + 配置固定标签 → B站标签串（最多 12 个，每个 20 字内）。"""
    tags = re.findall(r"#([^\s#]+)", _clean_text(desc))
    tags += [t.strip() for t in str(extra or "").split(",")]
    result = []
    for tag in tags:
        tag = re.sub(r"[,，]", "", tag).strip()
        if tag and tag not in result:
            result.append(tag[:20])
    return ",".join(result[:12])


def _load_state(files, local_path):
    raw = (files or {}).get(SYNCED_FILE)
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            util.log_warn(f"Gist {SYNCED_FILE} 格式错误，重新初始化：{e}")
            return {}
    if os.path.isfile(local_path):
        try:
            with open(local_path, encoding="utf8") as f:
                return json.load(f)
        except Exception as e:
            util.log_warn(f"本地状态文件格式错误，重新初始化：{e}")
    return {}


def _save_state(store, state, local_path, dry_run=False):
    if dry_run:
        util.log("[dry-run] 跳过写回同步状态")
        return
    if store:
        store.update(SYNCED_FILE, state)
        return
    with open(local_path, "w", encoding="utf8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _load_channels(files, local_path):
    raw = (files or {}).get(CHANNELS_FILE)
    source = "Gist"
    if not raw and os.path.isfile(local_path):
        with open(local_path, encoding="utf8") as f:
            raw = f.read()
        source = local_path
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception as e:
        util.log_error(f"博主页配置不是合法 JSON（{source}）：{e}")
        return []
    channels = data.get("channels") if isinstance(data, dict) else data
    channels = [c for c in channels or [] if isinstance(c, dict)]
    util.log(f"博主页配置来源：{source}，共 {len(channels)} 个")
    return channels


def _prepare_douyin_cookie(files, cfg):
    """准备 patchright 登录态文件：优先 Gist，其次本地显式路径/约定目录。"""
    account = str(cfg.get("douyin_account") or "").strip()
    explicit = cfg.get("douyin_cookie")
    if explicit and os.path.isfile(explicit):
        return explicit
    if account:
        gist_name = f"{DOUYIN_COOKIE_PREFIX}_{account}.json"
        content = (files or {}).get(gist_name)
        if content:
            os.makedirs(DOUYIN_COOKIE_DIR, exist_ok=True)
            path = os.path.join(DOUYIN_COOKIE_DIR, f"douyin_{account}.json")
            with open(path, "w", encoding="utf8") as f:
                f.write(content)
            util.log(f"抖音登录态已同步：Gist:{gist_name} -> {path}")
            return path
        for path in (os.path.join(DOUYIN_COOKIE_DIR, f"douyin_{account}.json"),
                     os.path.join(".sau", "cookies", f"douyin_{account}.json")):
            if os.path.isfile(path):
                return path
    return None


def _prepare_bili_cookie(files, cfg):
    """准备 B站登录态文件，返回 (本地路径, Gist 文件名)；找不到返回 (None, 名称)。"""
    gist_name = cfg.get("bili_cookie") or BILI_COOKIE_FILE
    local = "cookies.json" if gist_name == BILI_COOKIE_FILE else gist_name
    content = (files or {}).get(gist_name)
    if content is not None:
        with open(local, "w", encoding="utf8") as f:
            f.write(content)
        util.log(f"B站登录态已同步：Gist:{gist_name} -> {local}")
        return local, gist_name
    if os.path.isfile(local):
        util.log(f"B站登录态使用本地文件：{local}")
        return local, gist_name
    return None, gist_name


def _renew_bili_cookie(store, uploader, cookie_file, gist_name):
    """刷新 B站 cookie 并同步回 Gist，保持登录态新鲜（失败仅告警）。"""
    util.log(f"刷新 B站 cookies：{cookie_file}")
    try:
        subprocess.run([uploader.biliup_path, "-u", cookie_file, "renew"],
                       capture_output=True, timeout=180)
    except Exception as e:
        util.log_warn(f"刷新 B站 cookies 失败（不影响本次结果）：{e}")
        return
    if store and os.path.isfile(cookie_file):
        with open(cookie_file, encoding="utf8") as f:
            store.update(gist_name, json.load(f))


def _upload_one(downloader, uploader, cfg, post, seen):
    """下载并投稿单个作品；成功后把 bvid 记进 seen，失败抛异常且不记状态。"""
    vid = post["aweme_id"]
    video_file = f"{vid}.mp4"
    cover_file = f"{vid}.jpg"
    try:
        downloader.download(post, video_file)
        has_cover = False
        if post.get("cover_url"):
            try:
                has_cover = downloader.download_cover(post["cover_url"], cover_file)
            except Exception as e:
                util.log_warn(f"封面下载失败（不影响投稿）：{e}")
        title = _title_from_desc(post.get("desc"))
        tags = _tags_from_desc(post.get("desc"), cfg.get("tags"))
        source = post.get("url") or f"https://www.douyin.com/video/{vid}"
        util.log(f"投稿到 B站：title={title!r}，tags={tags!r}，来源={source}")
        ret = uploader.upload(
            video_file,
            title=title,
            tid=cfg.get("tid", 21),
            tags=tags,
            source=source,
            copyright=cfg.get("copyright", "2"),
            desc=cfg.get("desc"),
            cover=cover_file if has_cover else None,
        )
        if ret.get("code") != 0:
            raise RuntimeError(f"biliup 返回异常：{ret}")
        data = ret.get("data") or {}
        seen[vid] = {
            "bvid": data.get("bvid", ""),
            "aid": data.get("aid"),
            "t": post["create_time"],
            "at": time.strftime("%Y-%m-%d %H:%M"),
        }
        util.log(f"同步成功：{vid} -> bvid={data.get('bvid')}")
        return ret
    finally:
        downloader.cleanup(video_file, cover_file)


def sync_channel(cfg, files, store, state, *, dry_run=False, headless=False,
                 state_path=SYNCED_FILE):
    """同步一个抖音博主，返回 (成功条数, 失败条数)。"""
    name = str(cfg.get("name") or cfg.get("user") or cfg.get("sec_uid") or "").strip()
    user = cfg.get("user") or cfg.get("sec_uid")
    if not user:
        util.log_warn(f"博主配置缺少 user/sec_uid，跳过：{cfg}")
        return 0, 1
    sec_uid = DouyinDownloader.extract_sec_uid(user)
    util.log(f"===== 处理抖音博主：{name}（{sec_uid}）=====")

    storage = _prepare_douyin_cookie(files, cfg)
    if not storage:
        util.log_warn(f"博主 {name} 缺少抖音登录态（douyin_account 或 douyin_cookie），跳过")
        return 0, 1

    downloader = DouyinDownloader(
        storage_state=storage, headless=headless,
        proxy=cfg.get("proxy") or os.environ.get("DOUYIN_PROXY") or None,
        log=util.log)
    posts = downloader.list_posts(
        user, limit=int(cfg.get("limit") or 20), pages=int(cfg.get("pages") or 1))
    entry = state.setdefault(sec_uid, {"name": name, "seen": {}})
    entry["name"] = name
    seen = entry.setdefault("seen", {})

    # 首次运行：只记录基线，避免把博主历史作品一次性刷到 B站
    if not entry.get("baseline_at"):
        if not posts:
            util.log_warn(f"博主 {name} 当前没有可见作品，暂不写基线，下次再试")
            return 0, 0
        if not dry_run:
            for post in posts:
                if post["aweme_id"] not in seen:
                    seen[post["aweme_id"]] = {
                        "baseline": True, "t": post["create_time"],
                        "type": "image" if post["is_image"] else "video"}
            entry["baseline_at"] = int(time.time())
            _save_state(store, state, state_path)
            util.log(f"博主 {name} 首次运行，已记录 {len(posts)} 条基线作品（不推送），"
                     "之后只同步新作品")
        else:
            util.log(f"[dry-run] 博主 {name} 首次运行：将记录 {len(posts)} 条基线作品（不推送）")
        return 0, 0

    new_posts = [p for p in posts if p["aweme_id"] not in seen]
    images = [p for p in new_posts if p["is_image"]]
    if images and not dry_run:
        for post in images:
            seen[post["aweme_id"]] = {"skip": "image", "t": post["create_time"]}
        _save_state(store, state, state_path)
    if images:
        util.log(f"博主 {name} 新增 {len(images)} 条图文作品，B站不支持图文，记为跳过")

    pending = sorted((p for p in new_posts if not p["is_image"]),
                     key=lambda x: x["create_time"])
    if not pending:
        util.log(f"博主 {name} 没有新的视频作品")
        return 0, 0

    max_per_run = int(cfg.get("max_per_run") or 3)
    if dry_run:
        for post in pending[:max_per_run]:
            util.log(f"[dry-run] 将同步：{post['aweme_id']} "
                     f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(post['create_time']))} "
                     f"{_title_from_desc(post.get('desc'))!r}")
        return 0, 0

    bili_cookie, gist_name = _prepare_bili_cookie(files, cfg)
    if not bili_cookie:
        util.log_warn(f"博主 {name} 缺少 B站登录态（Gist:{gist_name}），跳过")
        return 0, 1
    uploader = BilibiliUploader(
        user_cookie=bili_cookie,
        default_copyright=str(cfg.get("copyright", "2")),
        default_desc=cfg.get("desc", DEFAULT_DESC),
        verify=VERIFY,
        log=util.log,
    )
    util.log(f"待同步 {len(pending)} 条，本次最多处理 {max_per_run} 条")

    uploaded = failed = 0
    for post in pending[:max_per_run]:
        util.log(f"----- 同步作品：{post['aweme_id']} {post['desc'][:40]!r} -----")
        try:
            _upload_one(downloader, uploader, cfg, post, seen)
            _save_state(store, state, state_path)
            uploaded += 1
        except Exception as e:
            util.log_warn(f"作品 {post['aweme_id']} 同步失败（下次重试）：{e}")
            failed += 1
            continue
        if uploaded < min(len(pending), max_per_run):
            util.log(f"防验证码/限流，暂停 {UPLOAD_SLEEP_SECOND} 秒")
            time.sleep(UPLOAD_SLEEP_SECOND)

    if uploaded:
        _renew_bili_cookie(store, uploader, bili_cookie, gist_name)
    return uploaded, failed


def main():
    parser = argparse.ArgumentParser(description="抖音博主 → B站 作品同步")
    parser.add_argument("token", nargs="?", default=None, help="github api token（本地调试可省略）")
    parser.add_argument("gistId", nargs="?", default=None, help="gist id（本地调试可省略）")
    parser.add_argument("--logLevel", default="INFO", help="日志级别：INFO/DEBUG")
    parser.add_argument("--channels", default=CHANNELS_FILE, help="博主页配置文件路径")
    parser.add_argument("--state-file", default=SYNCED_FILE, help="本地模式的状态文件路径")
    parser.add_argument("--channel", default=None, help="只处理指定博主（名称或 sec_uid）")
    parser.add_argument("--dry-run", action="store_true", help="只拉列表并打印，不下载/不投稿/不写状态")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式调试（抖音会反爬，CI 请用有头模式）")
    args = parser.parse_args()
    # 参数可能带首尾空白：bash 会自动丢弃，PowerShell 会原样传递，统一清理
    args.token = (args.token or "").strip()
    args.gistId = (args.gistId or "").strip()

    logging.basicConfig(
        stream=sys.stdout,
        level=getattr(logging, str(args.logLevel).upper(), logging.INFO),
        format="%(filename)s:%(lineno)d %(asctime)s.%(msecs)03d %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    store = None
    files = {}
    if args.token and args.gistId:
        store = GistStore(args.gistId, args.token, verify=VERIFY,
                          description="抖音→B站同步", log=util.log)
        files = store.fetch()
    else:
        util.log("未提供 GitHub token/gistId：本地模式，状态写本地文件，登录态读本地文件")

    channels = _load_channels(files, args.channels)
    if args.channel:
        def _matches(cfg):
            user = cfg.get("user") or cfg.get("sec_uid")
            keys = [str(cfg.get("name") or "")]
            if user:
                keys.append(DouyinDownloader.extract_sec_uid(user))
            return args.channel in keys
        channels = [c for c in channels if _matches(c)]
    if not channels:
        util.log("未配置待同步的抖音博主（douyin_channels.json），本次结束")
        return 0

    state = _load_state(files, args.state_file)
    total_uploaded = total_failed = 0
    for cfg in channels:
        if cfg.get("enabled") is False:
            util.log(f"博主 {cfg.get('name')} 已禁用，跳过")
            continue
        try:
            uploaded, failed = sync_channel(
                cfg, files, store, state,
                dry_run=args.dry_run, headless=args.headless,
                state_path=args.state_file)
        except Exception as e:
            util.log_error(f"博主 {cfg.get('name') or cfg.get('user')} 同步异常：{e}")
            uploaded, failed = 0, 1
        total_uploaded += uploaded
        total_failed += failed

    util.log(f"===== 同步结束：成功 {total_uploaded} 条，失败 {total_failed} 条 =====")
    return 1 if total_failed else 0


if __name__ == "__main__":
    sys.exit(main())

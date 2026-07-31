import json
import math
import os
import time
import requests
import xmltodict
import argparse
import logging
import sys

import google_util
import util
from bili_uploader import BilibiliUploader, read_uid
from gist_store import GistStore
from youtube_downloader import YoutubeDownloader
from video_processor import VideoProcessor

UPLOAD_SLEEP_SECOND = 60 * 2  # 2min
UPLOADED_VIDEO_FILE = "uploaded_video.json"
CONFIG_FILE = "config.json"
COOKIE_FILE = "cookie.json"
YT_COOKIE_FILE = "yt_cookies.txt"
GOOGLE_FILE = "google_credentials.json"
VERIFY = os.environ.get("verify", "1") == "1"
PROXY = {
    "https": os.environ.get("https_proxy", None)
}

# ===== 去水印(delogo)配置 =====
# 水印位置固定：左上角(tl) + 右上角(tr)，两侧横幅宽度不同，可分别配置
DELOGO_ENABLE = os.environ.get("delogo", "1") == "1"  # 设 delogo=0 可关闭
# 每个区域: corner=角落, w_ratio/h_ratio=模糊框占视频宽/高的比例
DELOGO_REGIONS = [
    {"corner": "tl", "w_ratio": 0.46, "h_ratio": 0.13},  # 左上：RUNNING MAN 横幅较宽
    {"corner": "tr", "w_ratio": 0.23, "h_ratio": 0.13},  # 右上：MYTV SUPER 台标
]
DELOGO_MARGIN = 6                    # 模糊框距离画面边缘的像素
DELOGO_CRF = "20"                    # 重编码质量(越小越清晰,18~23合理)
DELOGO_PRESET = "fast"               # 重编码速度预设

# ===== 抗查重(打破音视频指纹)配置 =====
# 在去水印同一次转码里附加：整体变速 + 音频变调 + 轻度裁剪缩放 + 清空元数据
# 目的：改变音频/画面指纹，降低B站版权查重命中率(注意：对正版内容无法保证通过)
ANTI_DETECT_ENABLE = os.environ.get("anti_detect", "0") == "1"  # 默认关闭；设 anti_detect=1 可开启
SPEED_FACTOR = 1.03                  # 音视频整体变速(1.03=快3%)，同步不跑偏
PITCH_FACTOR = 1.04                  # 音频额外变调倍数(1.04=音调升4%)
CROP_RATIO = 0.02                    # 四周各裁掉的比例(0.02=各裁2%)再缩放回原尺寸
STRIP_METADATA = True                # 清空mp4内嵌元数据(encoder/title/comment等)

# ===== 投稿字段默认值（表格未提供对应列时的回退）=====
DEFAULT_COPYRIGHT = "1"              # 1=自制, 2=转载
DEFAULT_DESC = "定期更新，喜欢的话求点赞投币关注！"


def _load_google_credentials(files):
    """读取 Google 服务账号凭据。

    优先从环境变量 GOOGLE_CREDENTIALS 读取（GitHub Actions Secret 注入）：Google 私钥
    绝不能存进 Gist，否则会被 GitHub secret scanning 扫到并上报 Google，导致 key 被
    自动停用（已泄露）。环境变量缺失时才回退 Gist 里的 google_credentials.json，仅为
    兼容尚未迁移的旧配置。"""
    env = os.environ.get("GOOGLE_CREDENTIALS")
    if env:
        util.log("Google 凭据来源：环境变量 GOOGLE_CREDENTIALS（Secret 注入）")
        return json.loads(env)
    raw = files.get(GOOGLE_FILE)
    if raw:
        util.log_warn("Google 凭据来源：Gist（不安全，会被扫描停用；请尽快改用 GOOGLE_CREDENTIALS Secret）")
        return json.loads(raw)
    raise Exception("缺少 Google 凭据：未设置环境变量 GOOGLE_CREDENTIALS，且 Gist 也无 google_credentials.json")


def load_gist(store):
    """从 Gist 读取全部文件，解析出 账号配置/已上传记录/Google 凭证，并同步 YouTube cookies。

    返回 (config, uploaded, google_json, files)；各账号的 B站 cookie 文件保留在 files 里，
    由 upload_process 按账号取用。Google 凭据优先取环境变量，见 _load_google_credentials。"""
    files = store.fetch()
    config = json.loads(files[CONFIG_FILE])
    google_json = _load_google_credentials(files)
    # 同步 YouTube cookies（Netscape 格式文本，直接写 cookies.txt）
    yt_cookie = files.get(YT_COOKIE_FILE)
    if yt_cookie:
        with open("cookies.txt", "w", encoding="utf8") as tmp:
            tmp.write(yt_cookie)
        util.log("YouTube cookies 已同步到本地")
    try:
        uploaded = json.loads(files.get(UPLOADED_VIDEO_FILE) or "{}")
    except Exception as e:
        util.log_error(f"uploaded_video 格式错误，重新初始化:{e}")
        uploaded = {}
    util.log(f"已上传视频记录数：{len(uploaded)}")
    util.log(f"账号配置数：{len(config)}")
    return config, uploaded, google_json, files


def get_video_list(channel_id: str):
    util.log(f"正在获取 YouTube RSS，channel_id={channel_id}")
    res = requests.get(
        "https://www.youtube.com/feeds/videos.xml?channel_id=" + channel_id).text
    res = xmltodict.parse(res)
    ret = []
    for elem in res.get("feed", {}).get("entry", []):
        no_emoji_title = util.clean(elem.get("title"))  # 去除表情
        str_list = no_emoji_title.split("#")  # 分割标签
        title = str_list[0]
        del str_list[0]
        ret.append({
            "vid": elem.get("yt:videoId"),
            "title": title,
            "origin": "https://www.youtube.com/watch?v=" + elem["yt:videoId"],
            "cover_url": elem["media:group"]["media:thumbnail"]["@url"],
            # "desc": elem["media:group"]["media:description"],
        })
    util.log(f"频道 {channel_id} 获取到 {len(ret)} 个视频")
    return ret


def _is_multi_account(cfg):
    """判断某个 config 项是否为多账号模式（同时具备 uid/worksheet/cookie_file）。"""
    return bool(cfg.get("uid") and cfg.get("worksheet") is not None and cfg.get("cookie_file"))


def upload_video(uploader, video_file, _config, detail):
    # 投稿字段优先级：表格(override) > config.json 默认 > uploader 内置默认
    # 标题截断、tid/copyright 取数字、dtime 解析等规则统一在 BilibiliUploader 内处理
    ov = detail.get("override", {})
    if ov:
        util.log(f"表格覆盖字段：{list(ov.keys())}")
    return uploader.upload(
        video_file,
        title=ov.get("title") or detail["title"],
        tid=ov.get("tid") or _config["tid"],
        tags=ov.get("tags") or _config["tags"],
        source=ov.get("source") or detail["origin"],
        copyright=ov.get("copyright"),
        desc=ov.get("desc"),
        dtime=ov.get("dtime"),
    )


def get_delay_time(count):
    hour = 1 * 60 * 60
    day = 24 * hour
    delay_time = day * count
    time_temp = math.floor(time.time() - 3 * hour + delay_time)
    return time_temp


def process_one(uploader, downloader, processor, detail, config, count):
    util.log(f'===== 开始处理第 {count} 个视频：{detail["vid"]} - {detail["title"]} =====')
    v_ext = downloader.download_with_fallback(detail["origin"], detail["vid"])
    if v_ext is None:
        util.log_error(f"所有格式均下载失败：{detail['vid']}")
        return False
    downloader.download_cover(detail["cover_url"], detail["vid"] + ".jpg")
    # 上传前对水印区域做模糊处理，规避B站版权检测
    processor.process(detail["vid"] + f".{v_ext}")
    util.log(f"开始上传到 B 站：{detail['vid']}.{v_ext}")
    ret = upload_video(uploader, detail["vid"] + f".{v_ext}", config, detail)
    util.log(f"上传完成，清理临时文件：{detail['vid']}.{v_ext}")
    os.remove(detail["vid"] + f".{v_ext}")
    return ret


def _prepare_account_cookie(cfg, files):
    """把该账号的登录态从 Gist 写到本地，供 biliup 使用。

    多账号：写 cfg['cookie_file'] 并校验 DedeUserID 与 uid 一致，返回 (cookie_file, ok)。
    legacy：把 Gist 的 COOKIE_FILE 写到 biliup 默认 cookies.json，返回 (None, True)。
    """
    if not _is_multi_account(cfg):
        with open("cookies.json", "w", encoding="utf8") as tmp:
            tmp.write(files.get(COOKIE_FILE) or "{}")
        util.log("B站 cookies 已写入本地文件（默认账号）")
        return None, True
    uid = str(cfg["uid"]).strip()
    cookie_file = cfg["cookie_file"]
    content = files.get(cookie_file)
    if content is None:
        util.log_warn(f"账号 {uid} 的 cookie 文件缺失于 Gist：{cookie_file}，跳过该账号")
        return cookie_file, False
    with open(cookie_file, "w", encoding="utf8") as tmp:
        tmp.write(content)
    actual = read_uid(cookie_file)
    if actual and actual != uid:
        util.log_warn(f"cookie {cookie_file} 实际 UID={actual} 与配置 uid={uid} 不一致，跳过该账号")
        return cookie_file, False
    util.log(f"账号 {uid} 登录态已就绪：{cookie_file}")
    return cookie_file, True


def _renew_and_sync_cookie(store, biliup_path, cookie_file):
    """刷新并把 B站 cookie 同步回 Gist；cookie_file 为 None 时用 biliup 默认 cookies.json。"""
    local = cookie_file or "cookies.json"
    gist_name = cookie_file or COOKIE_FILE
    util.log(f"刷新并同步 B站 cookies：{local} -> Gist:{gist_name}")
    if cookie_file:
        os.system(f"{biliup_path} -u {cookie_file} renew 2>&1 > /dev/null")
    else:
        os.system(f"{biliup_path} renew 2>&1 > /dev/null")
    with open(local, encoding="utf8") as tmp:
        data = tmp.read()
    store.update(gist_name, json.loads(data))
    os.remove(local)


def upload_process(gist_id, token):
    util.log("========== 上传流程开始 ==========")
    store = GistStore(gist_id, token, verify=VERIFY, description="大号数据", log=util.log)
    downloader = YoutubeDownloader(verify=VERIFY, log=util.log)
    processor = VideoProcessor(delogo=DELOGO_ENABLE, regions=DELOGO_REGIONS,
                               margin=DELOGO_MARGIN, crf=DELOGO_CRF, preset=DELOGO_PRESET,
                               anti_detect=ANTI_DETECT_ENABLE, speed_factor=SPEED_FACTOR,
                               pitch_factor=PITCH_FACTOR, crop_ratio=CROP_RATIO,
                               strip_metadata=STRIP_METADATA, log=util.log)
    util.log(f"yt-dlp 路径：{downloader.yt_dlp}")
    config, uploaded, google_json, files = load_gist(store)

    # 多账号：config 每项为一个账号；legacy：仅用首项、单数据源(worksheet 0)
    multi_mode = any(_is_multi_account(c) for c in config)
    accounts = config if multi_mode else config[:1]
    util.log(f"运行模式：{'多账号' if multi_mode else '单账号(legacy)'}，共 {len(accounts)} 个账号")

    count = 0
    for cfg in accounts:
        multi = _is_multi_account(cfg)
        if multi_mode and not multi:
            util.log_warn(f"配置项缺少 uid/worksheet/cookie_file，跳过：{cfg}")
            continue
        uid = str(cfg["uid"]).strip() if multi else None
        worksheet = cfg["worksheet"] if multi else 0
        label = f"账号 {uid}（tab={worksheet}）" if multi else "默认账号"
        util.log(f"===== 处理 {label} =====")

        # 准备该账号登录态
        cookie_file, ok = _prepare_account_cookie(cfg, files)
        if not ok:
            continue

        uploader = BilibiliUploader(
            user_cookie=cookie_file,
            default_copyright=str(cfg.get("copyright", DEFAULT_COPYRIGHT)),
            default_desc=cfg.get("desc", DEFAULT_DESC),
            log=util.log,
        )
        util.log(f"biliup 路径：{uploader.biliup_path}")

        # 取该账号 tab 的首个待处理视频
        detail = google_util.get_video_list_from_google(
            google_json, uploaded, worksheet=worksheet, uid=uid)
        if detail is None:
            util.log(f"{label}：无待处理视频")
            _renew_and_sync_cookie(store, uploader.biliup_path, cookie_file)
            continue
        key = detail["_dedup_key"]
        if uploaded.get(key) is not None:
            util.log(f"{label}：{detail['vid']} 已在 Gist 记录，跳过")
            _renew_and_sync_cookie(store, uploader.biliup_path, cookie_file)
            continue

        count += 1
        util.log(f"--- 第 {count} 个上传任务：{label} ---")
        row = detail.get("_row")
        col = detail.get("_status_col")
        ret = process_one(uploader, downloader, processor, detail, cfg, count)
        if not ret:
            util.log_warn(f"视频 {detail['vid']} 处理失败，跳过")
            if row and col:
                google_util.mark_row_status(
                    google_json, row, col, "失败 " + time.strftime("%Y-%m-%d %H:%M"),
                    worksheet=worksheet)
            _renew_and_sync_cookie(store, uploader.biliup_path, cookie_file)
            continue
        uploaded[key] = {"detail": detail, "config": cfg, "ret": ret}
        store.update(UPLOADED_VIDEO_FILE, uploaded)
        if row and col:
            bvid = ret.get("data", {}).get("bvid", "") if ret.get("data") else ""
            google_util.mark_row_status(
                google_json, row, col, f'{time.strftime("%Y-%m-%d %H:%M")} {bvid}'.strip(),
                worksheet=worksheet)
        util.log(
            f'上传完成,vid:{detail["vid"]},aid:{ret["data"]["aid"]},bvid:{ret["data"]["bvid"]}')
        # 刷新并同步该账号 cookie
        _renew_and_sync_cookie(store, uploader.biliup_path, cookie_file)
        util.log(f"防验证码，暂停 {UPLOAD_SLEEP_SECOND} 秒")
        time.sleep(UPLOAD_SLEEP_SECOND)

    if count == 0:
        util.log("没有需要上传的视频")
    util.log("========== 上传流程结束 ==========")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("token", help="github api token", type=str)
    parser.add_argument("gistId", help="gist id", type=str)
    args = parser.parse_args()
    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format='%(filename)s:%(lineno)d %(asctime)s.%(msecs)03d %(levelname)s: %(message)s',
        datefmt="%H:%M:%S",
    )
    upload_process(args.gistId, args.token)

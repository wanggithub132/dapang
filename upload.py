import json
import math
import os
import re
import shutil
import subprocess
import time
import requests
import xmltodict
import argparse
import logging
import sys

import google_util
import util

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



def get_gist(_gid, token):
    """通过 gist id 获取已上传数据"""
    util.log(f"正在获取 Gist 数据，gist_id={_gid}")
    rsp = requests.get(
        "https://api.github.com/gists/" + _gid,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
        },
        verify=VERIFY,
    )
    if rsp.status_code == 404:
        raise Exception("gist id 错误")
    if rsp.status_code == 403 or rsp.status_code == 401:
        raise Exception("github TOKEN 错误")
    util.log(f"Gist 请求成功，HTTP {rsp.status_code}")
    _data = rsp.json()
    file_names = list(_data.get("files", {}).keys())
    util.log(f"Gist 包含文件：{file_names}")
    uploaded_file = _data.get("files", {}).get(
        UPLOADED_VIDEO_FILE, {}).get("content", "{}")
    c = json.loads(_data["files"][CONFIG_FILE]["content"])
    t = json.loads(_data["files"][COOKIE_FILE]["content"])
    g_json = json.loads(_data["files"][GOOGLE_FILE]["content"])
    # 同步 YouTube cookies（Netscape 格式文本，直接写 cookies.txt）
    yt_cookie = _data.get("files", {}).get(YT_COOKIE_FILE, {}).get("content")
    if yt_cookie:
        with open("cookies.txt", "w", encoding="utf8") as tmp:
            tmp.write(yt_cookie)
        util.log("YouTube cookies 已同步到本地")
    try:
        u = json.loads(uploaded_file)
        util.log(f"已上传视频记录数：{len(u)}")
        util.log(f"频道配置数：{len(c)}")
        return c, t, u, g_json
    except Exception as e:
        util.log_error(f"gist 格式错误，重新初始化:{e}")
    return c, t, {},{}


def update_gist(_gid, token, file, data):
    util.log(f"正在更新 Gist 文件：{file}")
    rsp = requests.post(
        "https://api.github.com/gists/" + _gid,
        json={
            "description": "大号数据",
            "files": {
                file: {
                    "content": json.dumps(data, indent="  ", ensure_ascii=False)
                },
            }
        },
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
        },
        verify=VERIFY,
    )
    if rsp.status_code == 404:
        raise Exception("gist id 错误")
    if rsp.status_code == 422:
        raise Exception("github TOKEN 错误")
    util.log(f"Gist 更新成功，HTTP {rsp.status_code}")


def get_file_size(filename):
    sz = os.path.getsize(filename)
    return int(sz/1024/1024)


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


def select_not_uploaded(video_list: list, _uploaded: dict):
    util.log(f"筛选未上传视频：总候选 {len(video_list)} 个，已上传记录 {len(_uploaded)} 个")
    ret = []
    for i in video_list:
        if i["detail"]["vid"] == "5LT8Y_bgozs":
            continue
        if _uploaded.get(i["detail"]["vid"]) is not None:
            util.log_debug(f'vid:{i["detail"]["vid"]} 已被上传')
            continue
        elif "UC9h7Az08limpxBK7ycxS-SA" in i["config"]["channel_id"]:
            if "[Running man]" not in i["detail"]["title"]:  # 仅上传非 runningman
                util.log_debug(f'vid:{i["detail"]["vid"]} 不在需要上传的范围内')
                continue
        util.log(f'vid:{i["detail"]["vid"]} 待上传 - {i["detail"]["title"]}')
        ret.append(i)
    util.log(f"筛选完成：{len(ret)} 个视频需要上传")
    return ret


def get_all_video(_config,google_json):
    ret = []
    # for i in _config:
    #     res = get_video_list(i["channel_id"])
    #     for j in res:
    #         ret.append({
    #             "detail": j,
    #             "config": i
    #         })
        # 从Google表格中获取数据
    google_detail = google_util.get_video_list_from_google(google_json)
    if google_detail is not None:
        util.log(f"Google 表格获取到视频：{google_detail['vid']} - {google_detail['title']}")
        ret.append({
            "detail": google_detail,
            "config": _config[0]
        })
    else:
        util.log("Google 表格无待处理视频")
    util.log(f"视频汇总完成：共 {len(ret)} 个候选视频")
    return ret


YT_DLP = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
_biliup_dir = os.path.dirname(sys.executable)
_biliup_local = os.path.join(_biliup_dir, "biliup.exe" if os.name == "nt" else "biliup")
BILIUP = _biliup_local if os.path.isfile(_biliup_local) else shutil.which("biliup") or "biliup"


def download_video(url, out, format):
    util.log(f"开始下载视频：{url}，格式={format}，输出={out}")
    cmd = [YT_DLP, url, "-f", format, "-o", out]
    # 本地环境需要代理才能访问YouTube
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        cmd += ["--proxy", proxy]
    # GitHub Actions 等无浏览器环境使用 cookies.txt
    if os.path.isfile("cookies.txt"):
        cmd += ["--cookies", "cookies.txt"]
    # 使用 android 客户端减少反爬检测，降低 JS 依赖
    cmd += ["--extractor-args", "youtube:player_client=android,web"]
    # 增加重试
    cmd += ["--extractor-retries", "3"]
    util.log_debug(f"执行命令：{' '.join(cmd)}")
    try:
        msg = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=120)
        util.log_debug(msg[-512:])
        util.log(f"视频下载完毕，大小：{get_file_size(out)} MB")
        return True
    except subprocess.TimeoutExpired:
        util.log_warn(f"下载超时(120s)，跳过此格式")
        return False
    except subprocess.CalledProcessError as e:
        out = e.output.decode("utf8")
        if "This live event will begin in" in out:
            util.log("直播预告，跳过")
            return False
        if "Requested format is not available" in out:
            util.log_debug("视频无此类型：" + format)
            return False
        if "unable to download video data" in out or "HTTP Error 403" in out:
            util.log_warn(f"下载被拒绝(403/限流)，跳过此视频")
            return False
        if "page needs to be reloaded" in out or "Precondition check failed" in out:
            util.log_warn("YouTube API 限制，跳过此视频")
            return False
        util.log_error("未知错误:" + out)
        raise e


def download_cover(url, out):
    util.log(f"下载封面：{url} -> {out}")
    res = requests.get(url, verify=VERIFY).content
    with open(out, "wb") as tmp:
        tmp.write(res)
    util.log(f"封面下载完毕，大小：{len(res)} bytes")


def upload_video(video_file, _config, detail, count):
    title = detail['title']
    if len(title) > 80:
        util.log(f"标题超长({len(title)}字符)，截断为：{title[:80]}")
        title = title[:80]
    util.log(f"准备上传：{video_file}，标题={title}，分区tid={_config['tid']}")
    upload_cmd = [
        BILIUP, "upload",
        "--line", "ws",
        "--submit", "app",
        "--tid", str(_config['tid']),
        "--copyright", "2",
        "--title", title,
        "--tag", _config['tags'],
        "--source", detail['origin'],
        "--desc", "定期分享RunningMan 求赞求三连",
        video_file,
    ]
    util.log(f"调用 biliup 上传，路径={BILIUP}")
    util.log_debug(f"执行命令：{' '.join(upload_cmd)}")
    p = subprocess.Popen(
        upload_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out, err = p.communicate()
    util.log(f"biliup 进程结束，返回码={p.returncode}")
    if p.returncode != 0:
        err_text = err.decode("utf8", errors="replace") if err else ""
        out_text = out.decode("utf8", errors="replace") if out else ""
        raise Exception(f"biliup 失败(code={p.returncode}): {err_text}\n{out_text}")
    buf = out.splitlines(keepends=False)
    if len(buf) < 2:
        raise Exception(buf)
    try:
        data = buf[-2]
        data = data.decode()
    except Exception as e:
        util.log_error(f"输出结果错误:{buf}")
        raise e
    util.log_debug(f'上传完成，返回：{data}')
    # 解析 Rust Debug 格式: { code: 0, data: Some(Object { "aid": Number(...), ... }), message: "0", ttl: Some(1) }
    ret = {"code": -1, "data": None, "message": ""}
    m = re.search(r'code:\s*(-?\d+)', data)
    if m:
        ret["code"] = int(m.group(1))
    m = re.search(r'message:\s*"([^"]*)"', data)
    if m:
        ret["message"] = m.group(1)
    m = re.findall(r'"(\w+)":\s*(Number|String|\[?\w+\]?)\(([^)]*)\)', data)
    for key, val_type, val in m:
        if ret["data"] is None:
            ret["data"] = {}
        if val_type == "Number":
            ret["data"][key] = int(val.strip())
        elif val_type == "String":
            ret["data"][key] = val.strip('" ')
        else:
            ret["data"][key] = val.strip()
    return ret


def get_delay_time(count):
    hour = 1 * 60 * 60
    day = 24 * hour
    delay_time = day * count
    time_temp = math.floor(time.time() - 3 * hour + delay_time)
    return time_temp


def process_one(detail, config, count):
    util.log(f'===== 开始处理第 {count} 个视频：{detail["vid"]} - {detail["title"]} =====')
    formats = {"mp4": "b[ext=mp4]", "best": "b"}
    v_ext = None
    for ext, fmt in formats.items():
        util.log(f"尝试下载格式：{ext} ({fmt})")
        if download_video(detail["origin"], detail["vid"] + f".{ext}", fmt):
            v_ext = ext
            util.log(f"下载成功，使用格式：{ext}")
            break
    if v_ext is None:
        util.log_error(f"所有格式均下载失败：{detail['vid']}")
        return False
    download_cover(detail["cover_url"], detail["vid"] + ".jpg")
    util.log(f"开始上传到 B 站：{detail['vid']}.{v_ext}")
    ret = upload_video(detail["vid"] + f".{v_ext}",
                       config, detail, count)
    util.log(f"上传完成，清理临时文件：{detail['vid']}.{v_ext}")
    os.remove(detail["vid"] + f".{v_ext}")
    return ret


def upload_process(gist_id, token):
    util.log("========== 上传流程开始 ==========")
    util.log(f"yt-dlp 路径：{YT_DLP}")
    util.log(f"biliup 路径：{BILIUP}")
    config, cookie, uploaded ,google_json = get_gist(gist_id, token)
    with open("cookies.json", "w", encoding="utf8") as tmp:
        tmp.write(json.dumps(cookie))
    util.log("B站 cookies 已写入本地文件")
    need_to_process = get_all_video(config,google_json)
    need = select_not_uploaded(need_to_process, uploaded)
    if len(need) == 0:
        util.log("没有需要上传的视频")
    count = 0
    for i in need:
        count = count + 1
        util.log(f"--- 进度 {count}/{len(need)} ---")
        ret = process_one(i["detail"], i["config"], count)
        if not ret:
            util.log_warn(f"视频 {i['detail']['vid']} 处理失败，跳过")
            continue
        i["ret"] = ret
        uploaded[i["detail"]["vid"]] = i
        update_gist(gist_id, token, UPLOADED_VIDEO_FILE, uploaded)
        util.log(
            f'上传完成,vid:{i["detail"]["vid"]},aid:{ret["data"]["aid"]},bvid:{ret["data"]["bvid"]}')
        util.log(f"防验证码，暂停 {UPLOAD_SLEEP_SECOND} 秒")
        time.sleep(UPLOAD_SLEEP_SECOND)
    util.log("开始刷新 B站 cookies")
    os.system(f"{BILIUP} renew 2>&1 > /dev/null")
    with open("cookies.json", encoding="utf8") as tmp:
        data = tmp.read()
    update_gist(gist_id, token, COOKIE_FILE, json.loads(data))
    util.log("B站 cookies 已同步回 Gist")
    os.remove("cookies.json")
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

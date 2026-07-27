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
ANTI_DETECT_ENABLE = os.environ.get("anti_detect", "1") == "1"  # 设 anti_detect=0 可关闭
SPEED_FACTOR = 1.03                  # 音视频整体变速(1.03=快3%)，同步不跑偏
PITCH_FACTOR = 1.04                  # 音频额外变调倍数(1.04=音调升4%)
CROP_RATIO = 0.02                    # 四周各裁掉的比例(0.02=各裁2%)再缩放回原尺寸
STRIP_METADATA = True                # 清空mp4内嵌元数据(encoder/title/comment等)



def get_gist(_gid, token):
    """通过 gist id 获取已上传数据"""
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
    # 强制输出 mp4 容器
    cmd += ["--merge-output-format", "mp4"]
    # 下载 EJS 挑战脚本 + 增加重试
    cmd += ["--remote-components", "ejs:github"]
    cmd += ["--extractor-retries", "3"]
    # 确保 deno 在 PATH 中（本地 deno/ 目录）
    deno_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deno")
    if os.path.isdir(deno_dir):
        env = os.environ.copy()
        env["PATH"] = deno_dir + os.pathsep + env["PATH"]
    else:
        env = None
    util.log_debug(f"执行命令：{' '.join(cmd)}")
    try:
        msg = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=300, env=env)
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


def get_video_resolution(video_file):
    """用 ffprobe 获取视频宽高，返回 (width, height)"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("未找到 ffprobe")
    cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height",
           "-of", "csv=s=x:p=0", video_file]
    out = subprocess.check_output(cmd, timeout=30).decode("utf8", errors="replace").strip()
    # 可能返回多行，取第一行
    out = out.splitlines()[0]
    w, h = out.split("x")
    return int(w), int(h)


def get_audio_sample_rate(video_file):
    """用 ffprobe 获取音频采样率，无音频返回 None"""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        cmd = [ffprobe, "-v", "error", "-select_streams", "a:0",
               "-show_entries", "stream=sample_rate",
               "-of", "csv=p=0", video_file]
        out = subprocess.check_output(cmd, timeout=30).decode("utf8", errors="replace").strip()
        if not out:
            return None
        return int(out.splitlines()[0])
    except Exception:
        return None


def _even(n):
    """取不大于 n 的最近偶数(libx264 要求宽高为偶数)"""
    n = int(n)
    return n - (n % 2)


def build_video_filter(width, height, delogo_str):
    """构造视频滤镜链：delogo去水印 -> 轻度裁剪 -> 缩放回原尺寸 -> 变速"""
    parts = []
    if delogo_str:
        parts.append(delogo_str)
    if ANTI_DETECT_ENABLE:
        # 四周各裁 CROP_RATIO，再缩放回原(偶数)尺寸，改变画面指纹
        cw = _even(width * (1 - 2 * CROP_RATIO))
        ch = _even(height * (1 - 2 * CROP_RATIO))
        cx = int(width * CROP_RATIO)
        cy = int(height * CROP_RATIO)
        w2 = _even(width)
        h2 = _even(height)
        parts.append(f"crop={cw}:{ch}:{cx}:{cy}")
        parts.append(f"scale={w2}:{h2}")
        # 变速(setpts 缩短时间轴 => 播放变快)
        parts.append(f"setpts=PTS/{SPEED_FACTOR}")
    return ",".join(parts)


def build_audio_filter(sample_rate):
    """构造音频滤镜链：变调 + 变速，打破音频指纹并与视频变速同步"""
    if not ANTI_DETECT_ENABLE or not sample_rate:
        return ""
    # asetrate 升高采样率=> 音调+速度同时升 PITCH_FACTOR；aresample 复位采样率
    # 再用 atempo 把总速度校正到 SPEED_FACTOR(与视频一致)，此时音调净升 PITCH_FACTOR
    tempo = SPEED_FACTOR / PITCH_FACTOR
    return (f"asetrate={sample_rate}*{PITCH_FACTOR},"
            f"aresample={sample_rate},"
            f"atempo={tempo:.5f}")


def build_delogo_filter(width, height, regions):
    """根据分辨率和各区域配置，构造 delogo 滤镜串"""
    m = DELOGO_MARGIN
    filters = []
    for region in regions:
        corner = region["corner"]
        bw = max(16, int(width * region["w_ratio"]))
        bh = max(16, int(height * region["h_ratio"]))
        if corner == "tl":
            x, y = m, m
        elif corner == "tr":
            x, y = width - bw - m, m
        elif corner == "bl":
            x, y = m, height - bh - m
        elif corner == "br":
            x, y = width - bw - m, height - bh - m
        else:
            continue
        # delogo 要求区域在画面内且不贴边（需保留至少1px用于插值）
        x = max(1, x)
        y = max(1, y)
        if x + bw >= width:
            bw = width - x - 1
        if y + bh >= height:
            bh = height - y - 1
        if bw <= 0 or bh <= 0:
            continue
        filters.append(f"delogo=x={x}:y={y}:w={bw}:h={bh}")
    return ",".join(filters)


def delogo_video(video_file):
    """去水印 + 抗查重处理：去水印、变速、音频变调、轻度裁剪缩放、清元数据，覆盖原文件"""
    if not DELOGO_ENABLE and not ANTI_DETECT_ENABLE:
        util.log("去水印与抗查重均已关闭，跳过视频处理")
        return video_file
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        util.log_warn("未找到 ffmpeg，跳过视频处理")
        return video_file
    try:
        width, height = get_video_resolution(video_file)
    except Exception as e:
        util.log_warn(f"获取视频分辨率失败，跳过视频处理：{e}")
        return video_file

    # 去水印滤镜(可能被 delogo=0 关闭)
    delogo_str = build_delogo_filter(width, height, DELOGO_REGIONS) if DELOGO_ENABLE else ""
    # 视频滤镜链(去水印 + 裁剪缩放 + 变速)
    video_filter = build_video_filter(width, height, delogo_str)
    if not video_filter:
        util.log_warn("视频滤镜为空，跳过视频处理")
        return video_file
    # 音频滤镜链(变调 + 变速)
    sample_rate = get_audio_sample_rate(video_file) if ANTI_DETECT_ENABLE else None
    audio_filter = build_audio_filter(sample_rate)

    root, ext = os.path.splitext(video_file)
    tmp_out = root + "_processed" + ext
    cmd = [ffmpeg, "-y", "-i", video_file, "-vf", video_filter]
    if audio_filter:
        # 有音频滤镜 => 音频必须重编码
        cmd += ["-af", audio_filter, "-c:a", "aac", "-b:a", "128k"]
    else:
        # 无抗查重音频处理 => 直接复制音频
        cmd += ["-c:a", "copy"]
    cmd += ["-c:v", "libx264", "-preset", DELOGO_PRESET, "-crf", DELOGO_CRF]
    if ANTI_DETECT_ENABLE and STRIP_METADATA:
        cmd += ["-map_metadata", "-1"]
    cmd += [tmp_out]

    util.log(f"视频处理：分辨率 {width}x{height}")
    util.log(f"  视频滤镜：{video_filter}")
    if audio_filter:
        util.log(f"  音频滤镜：{audio_filter}")
    if ANTI_DETECT_ENABLE:
        util.log(f"  抗查重：变速x{SPEED_FACTOR} 变调x{PITCH_FACTOR} 裁剪{CROP_RATIO*100:.0f}% 清元数据={STRIP_METADATA}")
    util.log_debug(f"执行命令：{' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
    except subprocess.TimeoutExpired:
        util.log_error("ffmpeg 处理超时(30min)，使用原视频上传")
        if os.path.isfile(tmp_out):
            os.remove(tmp_out)
        return video_file
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf8", errors="replace") if e.stderr else ""
        util.log_error(f"ffmpeg 处理失败，使用原视频上传：{err[-500:]}")
        if os.path.isfile(tmp_out):
            os.remove(tmp_out)
        return video_file
    # 用处理后的文件替换原文件
    os.remove(video_file)
    os.rename(tmp_out, video_file)
    util.log(f"视频处理完成：{video_file}，大小 {get_file_size(video_file)} MB")
    return video_file


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
        "--copyright", "1",
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
    formats = {"mp4": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]", "default": "best"}
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
    # 上传前对水印区域做模糊处理，规避B站版权检测
    delogo_video(detail["vid"] + f".{v_ext}")
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

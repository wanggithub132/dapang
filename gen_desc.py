"""
gen_desc.py - 根据 YouTube 视频生成带时间戳的中文 B 站简介

用法:
    python gen_desc.py <YouTube-URL>
    python gen_desc.py <video-id>

原理:
    1. 用 yt-dlp 获取视频元数据（标题、频道、时长、原描述）
    2. 下载字幕（优先中文，回退英文）
    3. 将字幕分段，每段取一句代表性内容
    4. 若为英文则自动翻译成中文
    5. 输出结构化带时间戳的简介

依赖: yt-dlp, requests (均已安装)
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

import requests


def safe_print(text, file=None):
    """安全打印，兼容 Windows GBK 终端"""
    out = file or sys.stdout
    # 尝试直接用 bytes 绕过编码器
    try:
        # 强制用 utf-8 编码然后直接写 stdout 的底层 buffer
        if hasattr(out, "buffer"):
            out.buffer.write(text.encode("utf-8") + b"\n")
            out.buffer.flush()
        else:
            out.write(text + "\n")
    except (UnicodeEncodeError, AttributeError):
        # 兜底：替换掉 GBK 不支持的字符
        encoded = text.encode("gbk", errors="replace").decode("gbk", errors="replace")
        out.write(encoded + "\n")


def log(msg):
    safe_print(f"[gen_desc] {msg}", file=sys.stderr)


def get_ytdlp_path():
    """获取 yt-dlp 可执行路径，与 upload.py 逻辑一致"""
    _dir = os.path.dirname(sys.executable)
    exe = os.path.join(_dir, "yt-dlp")
    if os.name == "nt":
        if os.path.isfile(exe + ".exe"):
            return exe + ".exe"
        if os.path.isfile(exe):
            return exe
    return exe


def run_ytdlp(args, timeout=120):
    """运行 yt-dlp，返回 stdout"""
    yt_dlp = get_ytdlp_path()
    cmd = [yt_dlp] + args
    log(f"执行: {' '.join(cmd[-6:])}")
    try:
        result = subprocess.check_output(cmd, stderr=subprocess.PIPE, timeout=timeout)
        return result.decode("utf8", errors="replace")
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode("utf8", errors="replace") if e.stderr else ""
        raise RuntimeError(f"yt-dlp 失败: {err[:500]}")


def get_video_info(url):
    """获取视频元数据"""
    out = run_ytdlp(["--dump-json", "--skip-download", url])
    return json.loads(out.strip().splitlines()[-1])


def ms_to_ts(ms):
    """毫秒 -> mm:ss"""
    total_sec = ms / 1000
    m = int(total_sec // 60)
    s = int(total_sec % 60)
    return f"{m:02d}:{s:02d}"


def parse_vtt_time(t_str):
    """VTT 时间格式 00:01.360 或 00:00:01.360 -> 毫秒"""
    t_str = t_str.replace(",", ".")
    parts = t_str.split(":")
    if len(parts) == 2:
        m, s = parts
        h = 0
    elif len(parts) == 3:
        h, m, s = parts
    else:
        return 0
    return int(float(h) * 3600000 + float(m) * 60000 + float(s) * 1000)


def parse_srt_time(t_str):
    """SRT 时间格式 00:00:01,360 -> 毫秒"""
    return parse_vtt_time(t_str.replace(",", "."))


def parse_srt_or_vtt(filepath):
    """解析 SRT/VTT 字幕文件，返回 [(start_ms, end_ms, text), ...]"""
    entries = []
    with open(filepath, "r", encoding="utf8") as f:
        content = f.read()

    # 去除 VTT 头部
    if content.startswith("WEBVTT"):
        lines = content.splitlines()
        # 跳过头部直到第一个空行后的时间轴
        clean_lines = []
        in_header = True
        for line in lines:
            if in_header and line.strip() == "":
                in_header = False
                continue
            if not in_header:
                clean_lines.append(line)
        content = "\n".join(clean_lines)

    # 解析时间轴行: 00:00:01.360 --> 00:00:03.040
    # SRT: 00:00:01,360 --> 00:00:03,040
    pattern = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{3})"
    )
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        time_match = None
        text_lines = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if not time_match:
                m = pattern.search(line)
                if m:
                    time_match = (m.group(1), m.group(2))
                    continue
            # 跳过序号行
            if re.match(r"^\d+$", line):
                continue
            # 跳过 VTT 注释/样式
            if line.startswith("NOTE") or line.startswith("STYLE"):
                continue
            text_lines.append(line)
        if time_match and text_lines:
            t1 = parse_srt_time(time_match[0])
            t2 = parse_srt_time(time_match[1])
            text = " ".join(text_lines)
            # 去除歌词符号 ♪ 和 HTML 标签
            text = re.sub(r"[♪🎵🎶#]", "", text).strip()
            text = re.sub(r"<[^>]+>", "", text).strip()
            if text:
                entries.append((t1, t2, text))

    return entries


def translate_text(text, target="zh", retries=3):
    """调用 Google Translate 免费 API 翻译文本"""
    if not text.strip():
        return text
    # 检测是否包含非中文字符来决定是否需要翻译
    if re.search(r"[\u4e00-\u9fff]", text):
        return text  # 已有中文，直接返回

    url = "https://translate.googleapis.com/translate_a/single"
    params = {
        "client": "gtx",
        "sl": "auto",
        "tl": target,
        "dt": "t",
        "q": text,
    }
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                parts = []
                for sentence in data[0]:
                    if sentence[0]:
                        parts.append(sentence[0])
                return "".join(parts)
            elif resp.status_code == 429:
                log(f"翻译频率限制，等待 {2 ** attempt}s...")
                time.sleep(2 ** attempt)
                continue
            else:
                log(f"翻译返回 {resp.status_code}，重试 {attempt + 1}")
                time.sleep(1)
        except Exception as e:
            log(f"翻译请求失败: {e}")
            time.sleep(1)
    return text  # 失败时返回原文


def segment_subs(subs, num_sections=8):
    """将字幕分为 num_sections 段，每段取最长的第一句作为代表"""
    if not subs:
        return []

    total_duration = subs[-1][1]  # 最后一条的结束时间
    section_len = total_duration / num_sections

    segments = []
    section_idx = 0
    current_end = section_len

    for start, end, text in subs:
        if start >= current_end and section_idx < num_sections - 1:
            section_idx += 1
            current_end = section_len * (section_idx + 1)

        # 每个 section 只取第一条
        if len(segments) <= section_idx:
            segments.append((start, text))
        else:
            # 如果当前段还没取到文本，用更长的文本替换
            prev_start, prev_text = segments[section_idx]
            if len(text) > len(prev_text):
                segments[section_idx] = (start, text)

    # 确保所有段都有值
    result = []
    for i in range(num_sections):
        if i < len(segments):
            result.append(segments[i])
        else:
            ts = int(i * section_len)
            result.append((ts, "..."))

    return result


def wrap_text(text, width=40):
    """简单断行，避免单行过长"""
    lines = []
    while len(text) > width:
        # 在空格或标点处断行
        cut = width
        for pos in range(width, max(width - 15, 0) - 1, -1):
            if text[pos] in " .,!?;:，。！？；：":
                cut = pos + 1
                break
        lines.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        lines.append(text)
    return "\n".join(lines)


def generate_desc(segments):
    """生成最终的结构化 B 站简介"""
    nav_lines = []
    for i, (ts_ms, text) in enumerate(segments, 1):
        ts = ms_to_ts(ts_ms)
        # 截取文本前 30 字
        short_text = text[:60] if len(text) > 60 else text
        nav_lines.append(f"{ts} - {short_text}")

    nav_text = "\n".join(nav_lines)

    desc_parts = [
        "⏱ 内容导航：",
        nav_text,
        "",
        "====================================",
        "✅ 定期更新，喜欢的话求点赞投币关注！",
    ]

    return "\n".join(desc_parts)


def main():
    parser = argparse.ArgumentParser(
        description="从 YouTube 视频生成带时间戳的中文 B 站简介"
    )
    parser.add_argument("url", help="YouTube 视频 URL 或 video ID")
    parser.add_argument(
        "--langs",
        default="zh-Hans,zh-Hant,zh,en",
        help="字幕语言优先级，逗号分隔（默认: zh-Hans,zh-Hant,zh,en）",
    )
    parser.add_argument(
        "--sections", type=int, default=8,
        help="时间戳分段数（默认 8）"
    )
    parser.add_argument(
        "--no-translate", action="store_true",
        help="不做翻译，保留原文"
    )
    parser.add_argument(
        "--output", "-o",
        help="输出到文件，不指定则打印到终端"
    )
    args = parser.parse_args()

    # 标准化 URL
    url = args.url
    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={url}"

    # 1. 获取元数据
    log("正在获取视频信息...")
    info = get_video_info(url)
    title = info.get("title", "未知标题")
    channel = info.get("channel", info.get("uploader", "未知频道"))
    log(f"视频: {title}")
    log(f"频道: {channel}")

    # 2. 下载字幕
    lang_priority = [l.strip() for l in args.langs.split(",")]
    sub_file = None
    used_lang = None

    with tempfile.TemporaryDirectory() as tmpdir:
        # 先试手动字幕
        for langs in [lang_priority, ["en"]]:
            for try_auto in [False, True]:
                lang_str = ",".join(langs)
                try:
                    output_template = os.path.join(tmpdir, "sub")
                    sub_args = ["--skip-download"]
                    if try_auto:
                        sub_args.append("--write-auto-subs")
                    else:
                        sub_args.append("--write-subs")
                    sub_args += [
                        "--sub-langs", lang_str,
                        "--convert-subs", "srt",
                        "-o", output_template,
                        url,
                    ]
                    run_ytdlp(sub_args, timeout=120)

                    # 查找生成的字幕文件
                    for f in os.listdir(tmpdir):
                        if f.endswith(".srt"):
                            sub_file = os.path.join(tmpdir, f)
                            # 提取语言代码
                            base = f.replace(".srt", "")
                            parts = base.split(".")
                            if len(parts) >= 2:
                                used_lang = parts[-1]
                            break
                    if sub_file:
                        break
                except Exception as e:
                    log(f"字幕下载失败 ({'自动' if try_auto else '手动'}; {lang_str}): {e}")
                    continue
            if sub_file:
                break

        if not sub_file:
            log("警告: 未找到任何字幕，将仅使用视频元数据生成简介")
            segments = []
        else:
            log(f"使用字幕: {os.path.basename(sub_file)} (语言: {used_lang})")
            raw_subs = parse_srt_or_vtt(sub_file)
            log(f"解析到 {len(raw_subs)} 条字幕")

            # 3. 分段
            segments = segment_subs(raw_subs, num_sections=args.sections)

            # 4. 翻译英文分段为中
            if not args.no_translate:
                log("正在翻译英文分段内容...")
                translated = []
                for ts, text in segments:
                    if text and text != "...":
                        t = translate_text(text)
                        translated.append((ts, t))
                        time.sleep(0.3)  # 避免频率限制
                    else:
                        translated.append((ts, text))
                segments = translated

    # 5. 生成简介
    desc = generate_desc(segments)

    # 输出
    if args.output:
        with open(args.output, "w", encoding="utf8") as f:
            f.write(desc)
        log(f"已写入: {args.output}")
    else:
        safe_print("\n" + "=" * 50)
        safe_print("生成的 B 站简介：")
        safe_print("=" * 50)
        safe_print(desc)

    # 同时输出 JSON 格式方便程序调用
    result = {
        "title": info.get("title", ""),
        "channel": channel,
        "duration": info.get("duration", 0),
        "url": info.get("webpage_url", url),
        "description": desc,
        "segments": [{"time": ms_to_ts(ts), "time_ms": ts, "text": text}
                     for ts, text in segments] if segments else [],
    }
    return result


if __name__ == "__main__":
    result = main()

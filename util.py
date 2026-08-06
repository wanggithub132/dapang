import logging
import os
import re
import shutil
import subprocess


def log(msg):
    """同时 print + logging.info，方便本地调试和远端日志"""
    print(msg)
    logging.info(msg)


def log_warn(msg):
    """同时 print + logging.warning"""
    print(f"[WARNING] {msg}")
    logging.warning(msg)


def log_error(msg):
    """同时 print + logging.error"""
    print(f"[ERROR] {msg}")
    logging.error(msg)


def log_debug(msg):
    """同时 print + logging.debug"""
    print(f"[DEBUG] {msg}")
    logging.debug(msg)


# 去除所有表情
def clean(desstr, restr=''):
    # 过滤表情
    try:
        co = re.compile(u'['u'\U0001F300-\U0001F64F' u'\U0001F680-\U0001F6FF'u'\u2600-\u2B55]+')
    except re.error:
        co = re.compile(u'('u'\ud83c[\udf00-\udfff]|'u'\ud83d[\udc00-\ude4f\ude80-\udeff]|'u'[\u2600-\u2B55])+')
    return co.sub(restr, desstr)


def cli_path(p):
    """命令行参数用路径：文件名以 '-' 开头会被工具当成选项，加 './' 前缀规避。"""
    return "./" + p if isinstance(p, str) and p.startswith("-") else p


def sniff_image(path):
    """按文件头识别图片格式：jpeg/png/gif/webp，无法识别返回 None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return None
    if head.startswith(b"\xff\xd8"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"GIF8"):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return None


def ensure_jpeg(path, warn):
    """封面统一转成 JPEG（B站图床按 jpeg 编码，只认 jpeg/png/gif）。

    YouTube 缩略图常为 WebP（Google 内容协商，扩展名 .jpg 内容却是 webp），
    直接上传会被 B站判定非法图片(-400)；这里用 ffmpeg 就地转成 JPEG。
    转换失败仅告警，保留原文件（补封面失败不阻断主流程）。
    """
    fmt = sniff_image(path)
    if fmt == "jpeg":
        return
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        warn(f"封面为 {fmt or '未知'} 且未找到 ffmpeg，无法转 JPEG（B站补封面可能失败）")
        return
    tmp = path + ".tmp.jpg"
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", cli_path(path), "-q:v", "2", tmp],
            capture_output=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as e:
        warn(f"封面转 JPEG 失败（{e}），保留原文件")
        return
    if result.returncode != 0 or not os.path.isfile(tmp):
        warn("封面转 JPEG 失败，保留原文件")
        return
    os.replace(tmp, path)
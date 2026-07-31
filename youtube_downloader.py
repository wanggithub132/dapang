"""YouTube 视频/封面下载能力（yt-dlp 封装）。

零业务依赖，仅用标准库 + requests；可被其他项目单独拷走复用：
    from youtube_downloader import YoutubeDownloader
    dl = YoutubeDownloader()
    ext = dl.download_with_fallback("https://youtu.be/xxx", "out")  # 生成 out.mp4
    dl.download_cover(cover_url, "out.jpg")

内置：代理(环境变量自动识别)、cookies.txt、EJS 挑战脚本、deno PATH 注入、
常见失败原因分类(直播预告/格式不存在/403限流/API 限制)与多格式回退。
"""
import os
import sys
import shutil
import logging
import subprocess

import requests

_LOGGER = logging.getLogger(__name__)

# 多格式回退：优先 mp4，失败再退到 best。
DEFAULT_FORMATS = {
    "mp4": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
    "default": "best",
}


def _make_log(log):
    """统一日志入口：外部注入单参 log(msg) 则用之，否则回退标准库 logging。"""
    if log is not None:
        return log

    def _default(msg):
        _LOGGER.info(msg)

    return _default


def _cli_path(p):
    """命令行参数用路径：文件名以 '-' 开头会被工具当成选项，加 './' 前缀规避。"""
    return "./" + p if isinstance(p, str) and p.startswith("-") else p


def default_yt_dlp_path():
    """缺省探测 yt-dlp：sys.executable 同目录。"""
    return os.path.join(os.path.dirname(sys.executable), "yt-dlp")


def _file_size_mb(filename):
    return int(os.path.getsize(filename) / 1024 / 1024)


class YoutubeDownloader:
    """基于 yt-dlp 的视频下载器。"""

    def __init__(self, yt_dlp_path=None, *, cookies_file="cookies.txt",
                 deno_dir=None, proxy=None, extractor_retries=3,
                 timeout=300, verify=True, log=None):
        self.yt_dlp = yt_dlp_path or default_yt_dlp_path()
        self.cookies_file = cookies_file
        self.deno_dir = deno_dir
        self.proxy = proxy
        self.extractor_retries = extractor_retries
        self.timeout = timeout
        self.verify = verify
        self._log = _make_log(log)

    def _warn(self, msg):
        self._log(f"[WARN] {msg}")

    def _error(self, msg):
        self._log(f"[ERROR] {msg}")

    def _debug(self, msg):
        self._log(f"[DEBUG] {msg}")

    def download(self, url, out, format):
        """按指定 format 下载单个视频，成功返回 True；可预期失败返回 False。"""
        self._log(f"开始下载视频：{url}，格式={format}，输出={out}")
        cmd = [self.yt_dlp, url, "-f", format, "-o", _cli_path(out)]
        # 本地环境需要代理才能访问 YouTube
        proxy = self.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if proxy:
            cmd += ["--proxy", proxy]
        # 无浏览器环境使用 cookies.txt
        if self.cookies_file and os.path.isfile(self.cookies_file):
            cmd += ["--cookies", self.cookies_file]
        # 强制输出 mp4 容器
        cmd += ["--merge-output-format", "mp4"]
        # 下载 EJS 挑战脚本 + 增加重试
        cmd += ["--remote-components", "ejs:github"]
        cmd += ["--extractor-retries", str(self.extractor_retries)]
        # 确保 deno 在 PATH 中（本地 deno/ 目录）
        deno_dir = self.deno_dir if self.deno_dir is not None else os.path.join(os.getcwd(), "deno")
        if os.path.isdir(deno_dir):
            env = os.environ.copy()
            env["PATH"] = deno_dir + os.pathsep + env["PATH"]
        else:
            env = None
        self._debug(f"执行命令：{' '.join(cmd)}")
        try:
            msg = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=self.timeout, env=env)
            self._debug(msg[-512:])
            self._log(f"视频下载完毕，大小：{_file_size_mb(out)} MB")
            return True
        except subprocess.TimeoutExpired:
            self._warn(f"下载超时({self.timeout}s)，跳过此格式")
            return False
        except subprocess.CalledProcessError as e:
            out_text = e.output.decode("utf8")
            if "This live event will begin in" in out_text:
                self._log("直播预告，跳过")
                return False
            if "Requested format is not available" in out_text:
                self._debug("视频无此类型：" + format)
                return False
            if "unable to download video data" in out_text or "HTTP Error 403" in out_text:
                self._warn("下载被拒绝(403/限流)，跳过此视频")
                return False
            if "page needs to be reloaded" in out_text or "Precondition check failed" in out_text:
                self._warn("YouTube API 限制，跳过此视频")
                return False
            self._error("未知错误:" + out_text)
            raise e

    def download_with_fallback(self, url, out_prefix, formats=None):
        """按 formats 顺序逐个尝试下载，成功返回选中的扩展名(如 'mp4')，全失败返回 None。
        输出文件名为 out_prefix + '.' + 扩展名。"""
        formats = formats or DEFAULT_FORMATS
        for ext, fmt in formats.items():
            self._log(f"尝试下载格式：{ext} ({fmt})")
            if self.download(url, f"{out_prefix}.{ext}", fmt):
                self._log(f"下载成功，使用格式：{ext}")
                return ext
        return None

    def download_cover(self, url, out):
        """下载封面图到本地。"""
        self._log(f"下载封面：{url} -> {out}")
        res = requests.get(url, verify=self.verify).content
        with open(out, "wb") as tmp:
            tmp.write(res)
        self._log(f"封面下载完毕，大小：{len(res)} bytes")

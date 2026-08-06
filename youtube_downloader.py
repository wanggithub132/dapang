"""YouTube 视频/封面下载能力（yt-dlp 封装）。

零业务依赖，仅用标准库 + requests；可被其他项目单独拷走复用：
    from youtube_downloader import YoutubeDownloader
    dl = YoutubeDownloader()
    ext = dl.download_with_fallback("https://youtu.be/xxx", "out")  # 生成 out.mp4
    dl.download_cover(cover_url, "out.jpg")

内置：代理(环境变量自动识别/指定/显式禁用)、cookies.txt、EJS 挑战脚本、deno PATH 注入、
常见失败原因分类(直播预告/格式不存在/403限流/API 限制)与多格式回退；
可选：下载完整性校验(大小+时长，防残缺文件假成功)、分片级重试(--fragment-retries)、
下载级重试(--retries)。所有新增能力默认关闭/保守，接口向后兼容。
"""
import os
import sys
import shutil
import logging
import subprocess

import requests

from util import cli_path, ensure_jpeg, sniff_image

_LOGGER = logging.getLogger(__name__)

# 多格式回退：mp4 优先（上传兼容性好），失败退 1080p 高清（允许 vp9/webm），最后 best。
DEFAULT_FORMATS = {
    "mp4": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]",
    "1080": "bestvideo[height<=1080][vcodec!*=av01]+bestaudio/best",
    "default": "best",
}


def _make_log(log):
    """统一日志入口：外部注入单参 log(msg) 则用之，否则回退标准库 logging。"""
    if log is not None:
        return log

    def _default(msg):
        _LOGGER.info(msg)

    return _default


def default_yt_dlp_path():
    """缺省探测 yt-dlp：PATH 优先，其次 sys.executable 同目录。"""
    found = shutil.which("yt-dlp")
    if found:
        return found
    return os.path.join(os.path.dirname(sys.executable), "yt-dlp")


def _file_size_mb(filename):
    return int(os.path.getsize(filename) / 1024 / 1024)


def probe_duration(filename, ffprobe_path=None):
    """ffprobe 探测文件时长；ffprobe 缺失/失败返回 None。"""
    probe = ffprobe_path or shutil.which("ffprobe")
    if not probe:
        return None
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", filename],
            capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


class YoutubeDownloader:
    """基于 yt-dlp 的视频下载器。"""

    def __init__(self, yt_dlp_path=None, *, cookies_file="cookies.txt",
                 deno_dir=None, proxy=None, extractor_retries=3,
                 timeout=300, verify=True, log=None,
                 fragment_retries=10, retries=10,
                 ffprobe_path=None, min_size_mb=None, min_duration_s=None):
        """proxy 三态：None=自动识别环境变量；""=显式禁用；非空=指定代理。
        min_size_mb/min_duration_s: None=关闭完整性校验；设置后下载完校验，
        不过则删除残片并视为该格式失败（回退下一格式）。
        """
        self.yt_dlp = yt_dlp_path or default_yt_dlp_path()
        self.cookies_file = cookies_file
        self.deno_dir = deno_dir
        self.proxy = proxy
        self.extractor_retries = extractor_retries
        self.timeout = timeout
        self.verify = verify
        self.fragment_retries = fragment_retries
        self.retries = retries
        self.ffprobe_path = ffprobe_path
        self.min_size_mb = min_size_mb
        self.min_duration_s = min_duration_s
        self._log = _make_log(log)

    def _warn(self, msg):
        self._log(f"[WARN] {msg}")

    def _error(self, msg):
        self._log(f"[ERROR] {msg}")

    def _debug(self, msg):
        self._log(f"[DEBUG] {msg}")

    def _is_valid(self, out):
        """完整性校验：大小 + 时长（ffprobe 缺失时仅校验大小）。"""
        if not os.path.isfile(out):
            return False
        if self.min_size_mb is not None and os.path.getsize(out) < self.min_size_mb * 1024 * 1024:
            return False
        if self.min_duration_s is not None:
            duration = probe_duration(out, self.ffprobe_path)
            if duration is None or duration < self.min_duration_s:
                return False
        return True

    @staticmethod
    def _remove_output(out):
        """删除主文件及 yt-dlp 残留(.part/.ytdl 等)，避免残片污染下次下载。"""
        for candidate in (out, out + ".part", out + ".ytdl", out + ".temp"):
            try:
                os.remove(candidate)
            except OSError:
                pass

    def download(self, url, out, format):
        """按指定 format 下载单个视频，成功返回 True；可预期失败返回 False。"""
        self._log(f"开始下载视频：{url}，格式={format}，输出={out}")
        cmd = [self.yt_dlp, url, "-f", format, "-o", cli_path(out)]
        # 本地环境需要代理才能访问 YouTube；proxy="" 显式禁用（忽略环境变量）
        proxy = self.proxy
        if proxy is None:
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if proxy is not None:
            cmd += ["--proxy", proxy]
        # 无浏览器环境使用 cookies.txt
        if self.cookies_file and os.path.isfile(self.cookies_file):
            cmd += ["--cookies", self.cookies_file]
        # 强制输出 mp4 容器
        cmd += ["--merge-output-format", "mp4"]
        # 下载 EJS 挑战脚本 + 增加重试（下载级 + 分片级）
        cmd += ["--remote-components", "ejs:github"]
        cmd += ["--extractor-retries", str(self.extractor_retries)]
        cmd += ["--fragment-retries", str(self.fragment_retries)]
        cmd += ["--retries", str(self.retries)]
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
        except subprocess.TimeoutExpired:
            self._warn(f"下载超时({self.timeout}s)，跳过此格式")
            self._remove_output(out)
            return False
        except subprocess.CalledProcessError as e:
            out_text = e.output.decode("utf8", errors="replace")
            self._remove_output(out)
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
        # 下载成功后的完整性校验：残缺/空壳文件视为该格式失败（回退下一格式）
        if self.min_size_mb is not None or self.min_duration_s is not None:
            if not self._is_valid(out):
                self._warn(f"完整性校验失败(大小/时长不足)，删除残片：{out}")
                self._remove_output(out)
                return False
        self._log(f"视频下载完毕，大小：{_file_size_mb(out)} MB")
        return True

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
        """下载封面图到本地，并统一转成 JPEG（YouTube 缩略图常为 WebP）。"""
        self._log(f"下载封面：{url} -> {out}")
        res = requests.get(url, headers={"Accept": "image/jpeg"},
                           verify=self.verify).content
        with open(out, "wb") as tmp:
            tmp.write(res)
        ensure_jpeg(out, self._warn)
        fmt = sniff_image(out)
        self._log(f"封面下载完毕，大小：{len(res)} bytes，格式：{fmt}")

"""视频去水印 + 抗查重处理能力（ffmpeg 封装）。

零业务依赖，仅用标准库；可被其他项目单独拷走复用：
    from video_processor import VideoProcessor
    vp = VideoProcessor(regions=[{"corner": "tl", "w_ratio": 0.46, "h_ratio": 0.13}])
    vp.process("video.mp4")   # 就地覆盖原文件；失败/无 ffmpeg 时原样返回

能力：
    - 去水印(delogo)：按角落 + 宽高比例，在画面四角遮挡台标/横幅。
    - 抗查重(可选)：整体变速 + 音频变调 + 轻度裁剪缩放 + 清空元数据，
      改变音视频指纹（注意：对正版内容无法保证通过平台查重）。
需要系统已安装 ffmpeg / ffprobe（通过 PATH 探测）。
"""
import os
import shutil
import logging
import subprocess

_LOGGER = logging.getLogger(__name__)

# 水印位置默认：左上角(tl) + 右上角(tr)，可按需覆盖。
DEFAULT_REGIONS = [
    {"corner": "tl", "w_ratio": 0.46, "h_ratio": 0.13},
    {"corner": "tr", "w_ratio": 0.23, "h_ratio": 0.13},
]


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


def _even(n):
    """取不大于 n 的最近偶数(libx264 要求宽高为偶数)。"""
    n = int(n)
    return n - (n % 2)


class VideoProcessor:
    """基于 ffmpeg 的视频去水印 + 抗查重处理器。"""

    def __init__(self, *, delogo=True, regions=None, margin=6, crf="20", preset="fast",
                 anti_detect=False, speed_factor=1.03, pitch_factor=1.04,
                 crop_ratio=0.02, strip_metadata=True, timeout=1800, log=None):
        self.delogo = delogo
        self.regions = regions if regions is not None else DEFAULT_REGIONS
        self.margin = margin
        self.crf = crf
        self.preset = preset
        self.anti_detect = anti_detect
        self.speed_factor = speed_factor
        self.pitch_factor = pitch_factor
        self.crop_ratio = crop_ratio
        self.strip_metadata = strip_metadata
        self.timeout = timeout
        self._log = _make_log(log)

    def _warn(self, msg):
        self._log(f"[WARN] {msg}")

    def _error(self, msg):
        self._log(f"[ERROR] {msg}")

    def _debug(self, msg):
        self._log(f"[DEBUG] {msg}")

    # ---------- ffprobe 探测 ----------
    def get_video_resolution(self, video_file):
        """用 ffprobe 获取视频宽高，返回 (width, height)。"""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            raise RuntimeError("未找到 ffprobe")
        cmd = [ffprobe, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height",
               "-of", "csv=s=x:p=0", _cli_path(video_file)]
        out = subprocess.check_output(cmd, timeout=30).decode("utf8", errors="replace").strip()
        # 可能返回多行，取第一行
        out = out.splitlines()[0]
        w, h = out.split("x")
        return int(w), int(h)

    def get_audio_sample_rate(self, video_file):
        """用 ffprobe 获取音频采样率，无音频返回 None。"""
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return None
        try:
            cmd = [ffprobe, "-v", "error", "-select_streams", "a:0",
                   "-show_entries", "stream=sample_rate",
                   "-of", "csv=p=0", _cli_path(video_file)]
            out = subprocess.check_output(cmd, timeout=30).decode("utf8", errors="replace").strip()
            if not out:
                return None
            return int(out.splitlines()[0])
        except Exception:
            return None

    # ---------- 滤镜构造 ----------
    def build_delogo_filter(self, width, height):
        """根据分辨率和各区域配置，构造 delogo 滤镜串。"""
        m = self.margin
        filters = []
        for region in self.regions:
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

    def build_video_filter(self, width, height, delogo_str):
        """构造视频滤镜链：delogo去水印 -> 轻度裁剪 -> 缩放回原尺寸 -> 变速。"""
        parts = []
        if delogo_str:
            parts.append(delogo_str)
        if self.anti_detect:
            # 四周各裁 crop_ratio，再缩放回原(偶数)尺寸，改变画面指纹
            cw = _even(width * (1 - 2 * self.crop_ratio))
            ch = _even(height * (1 - 2 * self.crop_ratio))
            cx = int(width * self.crop_ratio)
            cy = int(height * self.crop_ratio)
            w2 = _even(width)
            h2 = _even(height)
            parts.append(f"crop={cw}:{ch}:{cx}:{cy}")
            parts.append(f"scale={w2}:{h2}")
            # 变速(setpts 缩短时间轴 => 播放变快)
            parts.append(f"setpts=PTS/{self.speed_factor}")
        return ",".join(parts)

    def build_audio_filter(self, sample_rate):
        """构造音频滤镜链：变调 + 变速，打破音频指纹并与视频变速同步。"""
        if not self.anti_detect or not sample_rate:
            return ""
        # asetrate 升高采样率=> 音调+速度同时升 pitch_factor；aresample 复位采样率
        # 再用 atempo 把总速度校正到 speed_factor(与视频一致)，此时音调净升 pitch_factor
        tempo = self.speed_factor / self.pitch_factor
        return (f"asetrate={sample_rate}*{self.pitch_factor},"
                f"aresample={sample_rate},"
                f"atempo={tempo:.5f}")

    # ---------- 主流程 ----------
    def process(self, video_file):
        """去水印 + 抗查重处理，就地覆盖原文件；任何不可用/失败情况下原样返回文件名。"""
        if not self.delogo and not self.anti_detect:
            self._log("去水印与抗查重均已关闭，跳过视频处理")
            return video_file
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self._warn("未找到 ffmpeg，跳过视频处理")
            return video_file
        try:
            width, height = self.get_video_resolution(video_file)
        except Exception as e:
            self._warn(f"获取视频分辨率失败，跳过视频处理：{e}")
            return video_file

        # 去水印滤镜(可能被关闭)
        delogo_str = self.build_delogo_filter(width, height) if self.delogo else ""
        # 视频滤镜链(去水印 + 裁剪缩放 + 变速)
        video_filter = self.build_video_filter(width, height, delogo_str)
        if not video_filter:
            self._warn("视频滤镜为空，跳过视频处理")
            return video_file
        # 音频滤镜链(变调 + 变速)
        sample_rate = self.get_audio_sample_rate(video_file) if self.anti_detect else None
        audio_filter = self.build_audio_filter(sample_rate)

        root, ext = os.path.splitext(video_file)
        tmp_out = root + "_processed" + ext
        cmd = [ffmpeg, "-y", "-i", _cli_path(video_file), "-vf", video_filter]
        if audio_filter:
            # 有音频滤镜 => 音频必须重编码
            cmd += ["-af", audio_filter, "-c:a", "aac", "-b:a", "128k"]
        else:
            # 无抗查重音频处理 => 直接复制音频
            cmd += ["-c:a", "copy"]
        cmd += ["-c:v", "libx264", "-preset", self.preset, "-crf", self.crf]
        if self.anti_detect and self.strip_metadata:
            cmd += ["-map_metadata", "-1"]
        cmd += [_cli_path(tmp_out)]

        self._log(f"视频处理：分辨率 {width}x{height}")
        self._log(f"  视频滤镜：{video_filter}")
        if audio_filter:
            self._log(f"  音频滤镜：{audio_filter}")
        if self.anti_detect:
            self._log(f"  抗查重：变速x{self.speed_factor} 变调x{self.pitch_factor} "
                      f"裁剪{self.crop_ratio*100:.0f}% 清元数据={self.strip_metadata}")
        self._debug(f"执行命令：{' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self._error(f"ffmpeg 处理超时({self.timeout}s)，使用原视频上传")
            if os.path.isfile(tmp_out):
                os.remove(tmp_out)
            return video_file
        except subprocess.CalledProcessError as e:
            err = e.stderr.decode("utf8", errors="replace") if e.stderr else ""
            self._error(f"ffmpeg 处理失败，使用原视频上传：{err[-500:]}")
            if os.path.isfile(tmp_out):
                os.remove(tmp_out)
            return video_file
        # 用处理后的文件替换原文件
        os.remove(video_file)
        os.rename(tmp_out, video_file)
        self._log(f"视频处理完成：{video_file}")
        return video_file

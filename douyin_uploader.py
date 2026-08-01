"""抖音视频上传能力（封装 social-auto-upload 的 sau CLI）。

零业务依赖，仅用标准库；可被其他项目单独拷走复用：
    from douyin_uploader import DouyinUploader
    ret = DouyinUploader(sau_dir=".sau", account="my").upload(
        "a.mp4", title="标题", tags="标签1,标签2")

约定：cookie 沿用 social-auto-upload 惯例，从 <sau_dir>/cookies/douyin_<账号>.json 读取
    （Playwright storage_state 格式）；sau 上传成功后会刷新该文件，调用方可回写远端
    保持登录态新鲜。上传依赖 patchright 自带 Chromium 内核（sau douyin login 时下载）。
"""
import os
import sys
import logging
import subprocess

_LOGGER = logging.getLogger(__name__)

DOUYIN_MAX_TITLE_LEN = 30  # 抖音标题最长 30 字（超出会被工具静默截断，这里提前截断并提示）
DOUYIN_MAX_TAGS = 5        # 抖音话题最多带 5 个
DOUYIN_TIMEOUT = 30 * 60   # 单次上传超时 30 分钟（浏览器上传较慢）


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


class DouyinUploader:
    """封装 social-auto-upload 的 sau CLI 的一次抖音投稿提交。"""

    def __init__(self, sau_dir=".sau", account=None, timeout=DOUYIN_TIMEOUT,
                 default_desc="转载自 YouTube，喜欢的话求点赞关注！", log=None):
        self.sau_dir = sau_dir
        self.account = account
        self.timeout = timeout
        self.default_desc = default_desc
        self._log = _make_log(log)

    def _warn(self, msg):
        self._log(f"[WARN] {msg}")

    def _debug(self, msg):
        self._log(f"[DEBUG] {msg}")

    def cookie_path(self, account=None):
        """本地 cookie 文件路径（sau 约定：cookies/douyin_<账号>.json）。"""
        return os.path.join(self.sau_dir, "cookies", f"douyin_{account or self.account}.json")

    def upload(self, video_file, *, title, tags="", desc=None, thumbnail=None, account=None):
        """提交一个视频到抖音。

        title 超 30 字自动截断；tags 逗号分隔、最多取 5 个；
        thumbnail 为横版封面路径，文件不存在时忽略（抖音自动选推荐帧）。
        成功返回刷新后的 cookie 文件路径（供调用方回写远端）；失败抛异常。
        """
        acc = account or self.account
        if not acc:
            raise ValueError("缺少抖音账号标识（account），无法上传")
        title = (title or "").strip()
        if not title:
            raise ValueError("title 是必须的")
        if len(title) > DOUYIN_MAX_TITLE_LEN:
            self._log(f"标题超长({len(title)}字符)，截断为：{title[:DOUYIN_MAX_TITLE_LEN]}")
            title = title[:DOUYIN_MAX_TITLE_LEN]
        desc = desc if desc not in (None, "") else self.default_desc
        tags = [t.strip() for t in str(tags or "").split(",") if t.strip()][:DOUYIN_MAX_TAGS]

        # sau_cli.py 在 sau_dir 下运行，文件必须用绝对路径，否则相对路径会在 .sau 里找不到
        video_file = os.path.abspath(video_file)
        if not os.path.isfile(video_file):
            raise FileNotFoundError(f"视频文件不存在：{video_file}")

        self._log(f"准备上传：{video_file}，标题={title}，话题={tags}")
        cmd = [
            sys.executable, "sau_cli.py", "douyin", "upload-video",
            "--account", acc,
            "--file", _cli_path(video_file),
            "--title", title,
            "--desc", desc,
            "--tags", ",".join(tags),
        ]
        # 横版封面：文件存在才传；缺失时让抖音自动选推荐帧（不阻断）
        if thumbnail and os.path.isfile(thumbnail):
            cmd += ["--thumbnail-landscape", os.path.abspath(thumbnail)]
            self._log(f"抖音封面：{thumbnail}")
        else:
            self._warn("未找到封面文件，抖音将自动选推荐封面")
        self._debug(f"执行命令：{' '.join(cmd)}（cwd={self.sau_dir}）")
        try:
            p = subprocess.run(cmd, cwd=self.sau_dir, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=self.timeout)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"sau_cli.py 不存在于 {self.sau_dir}"
                "（social-auto-upload 未 clone，或依赖未安装到当前 Python 环境）")
        self._log(f"sau 进程结束，返回码={p.returncode}")
        if p.returncode != 0:
            tail = "\n".join((p.stdout or "").splitlines()[-5:] + (p.stderr or "").splitlines()[-5:])
            raise RuntimeError(f"sau 失败(code={p.returncode})：\n{tail}")
        self._log("抖音上传成功")
        return self.cookie_path(acc)

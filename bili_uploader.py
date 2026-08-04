"""B站视频上传能力（biliup 提交 + 官方接口补封面）。

零业务依赖（仅标准库 + requests），可被其他项目单独拷走复用：
    from bili_uploader import BilibiliUploader
    ret = BilibiliUploader().upload("a.mp4", title="标题", tid=21, tags="标签1,标签2")
    ret = BilibiliUploader().upload("a.mp4", title="标题", cover="a.jpg")  # 成功后自动补封面

约定：cookie 沿用 biliup 惯例，默认从运行目录读取 cookies.json；多账号时可用
    user_cookie 参数为每个账号指定各自的 cookie 文件。read_uid() 可从 cookie 读出 B站 UID。
封面说明：biliup v0.2.4 的 --cover 会触发 B站 -400（仓库已归档不再修复），
    故封面改为投稿成功后走官方接口补传：x/article/cover 图床上传 →
    x/vu/client/edit 更新稿件封面；补封面失败仅告警，不阻断已发布的视频。
"""
import os
import re
import sys
import time
import json
import shutil
import logging
import datetime
import subprocess

import requests

_LOGGER = logging.getLogger(__name__)


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


def default_biliup_path():
    """缺省探测 biliup 可执行文件：sys.executable 同目录优先，其次 PATH。"""
    d = os.path.dirname(sys.executable)
    local = os.path.join(d, "biliup.exe" if os.name == "nt" else "biliup")
    if os.path.isfile(local):
        return local
    return shutil.which("biliup") or "biliup"


def parse_dtime(s, log_warn=None):
    """将定时发布值解析为 10 位时间戳；空值/无法解析返回 None。
    支持：10位时间戳、'YYYY-MM-DD HH:MM(:SS)'、'YYYY/MM/DD HH:MM'、'YYYY-MM-DD'。"""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    if s.isdigit() and len(s) >= 10:
        return int(s[:10])
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
        try:
            return int(time.mktime(datetime.datetime.strptime(s, fmt).timetuple()))
        except ValueError:
            continue
    if log_warn:
        log_warn(f"定时发布值无法解析，将立即发布：{s}")
    return None


def read_uid(cookie_file):
    """从 biliup 的 cookie 文件读取 B站 UID(DedeUserID)；解析失败/找不到返回 None。
    biliup cookies.json 结构：{"cookie_info": {"cookies": [{"name": "DedeUserID", "value": "..."}, ...]}}"""
    try:
        with open(cookie_file, encoding="utf8") as f:
            data = json.load(f)
    except Exception:
        return None
    cookies = (data.get("cookie_info") or {}).get("cookies") or []
    for c in cookies:
        if c.get("name") == "DedeUserID":
            return str(c.get("value") or "").strip() or None
    return None


def _cookie_dict(cookie_file):
    """biliup cookies.json → {name: value}；解析失败返回空 dict。"""
    try:
        with open(cookie_file, encoding="utf8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out = {}
    for c in (data.get("cookie_info") or {}).get("cookies") or []:
        if c.get("name") is not None and c.get("value") is not None:
            out[c["name"]] = c["value"]
    return out


def _cover_headers(cookies):
    """图床/编辑接口公共 header：Cookie 串 + 浏览器 UA + 投稿页 Referer。"""
    return {
        "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Referer": "https://member.bilibili.com/platform/upload/video/frame.html",
    }


def _parse_biliup_output(data):
    """解析 biliup 的 Rust Debug 输出：
    { code: 0, data: Some(Object { "aid": Number(...), ... }), message: "0", ttl: Some(1) }"""
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


class BilibiliUploader:
    """封装 biliup CLI 的一次投稿提交。"""

    def __init__(self, biliup_path=None, line="ws", submit="app",
                 default_copyright="1",
                 default_desc="定期更新，喜欢的话求点赞投币关注！",
                 user_cookie=None,
                 verify=True,
                 log=None):
        self.biliup_path = biliup_path or default_biliup_path()
        self.line = line
        self.submit = submit
        self.default_copyright = str(default_copyright)
        self.default_desc = default_desc
        self.user_cookie = user_cookie
        self.verify = verify
        self._log = _make_log(log)

    def _warn(self, msg):
        self._log(f"[WARN] {msg}")

    def _error(self, msg):
        self._log(f"[ERROR] {msg}")

    def _debug(self, msg):
        self._log(f"[DEBUG] {msg}")

    def upload(self, video_file, *, title, tid, tags, source="",
               copyright=None, desc=None, dtime=None, cover=None):
        """提交一个视频到 B 站。

        title 超 80 字自动截断；tid/copyright 取纯数字；dtime 可解析才传 --dtime。
        cover：本地封面文件路径；投稿成功后自动走官方接口补封面（失败仅告警）。
        成功返回 {code, data:{aid, bvid, ...}, message}；失败抛异常。
        """
        title = title or ""
        if len(title) > 80:
            self._log(f"标题超长({len(title)}字符)，截断为：{title[:80]}")
            title = title[:80]
        # tid / copyright 提取纯数字，兼容 “164（运动·健身）” 这类写法
        tid = re.sub(r"[^0-9]", "", str(tid)) or str(tid)
        cr_raw = copyright if copyright not in (None, "") else self.default_copyright
        copyright = re.sub(r"[^0-9]", "", str(cr_raw)) or self.default_copyright
        desc = desc if desc not in (None, "") else self.default_desc

        self._log(f"准备上传：{video_file}，标题={title}，分区tid={tid}，copyright={copyright}")
        upload_cmd = [self.biliup_path]
        # 全局选项 -u 指定登录态文件（多账号时每个账号一份 cookie），须在子命令之前
        if self.user_cookie:
            upload_cmd += ["-u", self.user_cookie]
        upload_cmd += [
            "upload",
            "--line", self.line,
            "--submit", self.submit,
            "--tid", str(tid),
            "--copyright", str(copyright),
            "--title", title,
            "--tag", tags,
            "--source", source,
            "--desc", desc,
            _cli_path(video_file),
        ]
        # 定时发布：可解析时才传 --dtime（需距提交大于2小时，B站约限在15天内）
        ts = parse_dtime(dtime, self._warn)
        if ts:
            upload_cmd[-1:-1] = ["--dtime", str(ts)]
            self._log(f"启用定时发布，dtime={ts}")
        self._log(f"调用 biliup 上传，路径={self.biliup_path}")
        self._debug(f"执行命令：{' '.join(upload_cmd)}")
        p = subprocess.Popen(
            upload_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = p.communicate()
        self._log(f"biliup 进程结束，返回码={p.returncode}")
        if p.returncode != 0:
            err_text = err.decode("utf8", errors="replace") if err else ""
            out_text = out.decode("utf8", errors="replace") if out else ""
            raise Exception(f"biliup 失败(code={p.returncode}): {err_text}\n{out_text}")
        buf = out.splitlines(keepends=False)
        if len(buf) < 2:
            raise Exception(buf)
        try:
            data = buf[-2].decode()
        except Exception as e:
            self._error(f"输出结果错误:{buf}")
            raise e
        self._debug(f'上传完成，返回：{data}')
        ret = _parse_biliup_output(data)
        # 补封面：biliup 的 --cover 在 v0.2.4 有 -400 bug（仓库已归档），
        # 改为投稿成功后用官方接口补传；失败仅告警，不影响已发布的视频
        if ret.get("code") == 0 and cover:
            aid = ((ret.get("data") or {}).get("aid")) or ""
            if aid:
                try:
                    self.upload_cover(
                        aid, cover, title=title, tid=tid, tags=tags,
                        source=source, copyright=copyright, desc=desc, dtime=dtime)
                except Exception as e:
                    self._warn(f"补封面失败（不影响已发布的视频）：{e}")
        return ret

    def upload_cover(self, aid, cover_file, *, title="", tid="", tags="",
                     source="", copyright=None, desc=None, dtime=None):
        """为已投稿的 aid 补封面：B站图床上传 → 编辑接口更新。

        封面文件不存在 / cookie 缺 bili_jct 时返回 False（不抛异常）；
        接口失败抛异常，由调用方决定是否阻断。
        """
        cookie_file = self.user_cookie or "cookies.json"
        if not os.path.isfile(cover_file):
            self._warn(f"封面文件不存在，跳过：{cover_file}")
            return False
        cookies = _cookie_dict(cookie_file)
        csrf = cookies.get("bili_jct") or cookies.get("csrf")
        if not csrf:
            self._warn("cookie 中无 bili_jct，无法补封面")
            return False
        headers = _cover_headers(cookies)
        # 1) 封面图上传到 B站图床，返回可用的封面 URL
        with open(cover_file, "rb") as f:
            resp = requests.post(
                "https://api.bilibili.com/x/article/cover",
                headers=headers, data={"csrf": csrf}, files={"file": f},
                timeout=30, verify=self.verify)
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"封面上传失败：{data}")
        cover_url = ((data.get("data") or {}).get("url") or "").replace("http://", "https://")
        if not cover_url:
            raise Exception(f"封面上传无返回 URL：{data}")
        # 2) 编辑稿件更新封面（全量字段，与投稿参数保持一致）
        edit = {
            "aid": str(aid),
            "cover": cover_url,
            "csrf": csrf,
            "title": title,
            "tid": re.sub(r"[^0-9]", "", str(tid)) or str(tid),
            "tag": tags,
            "copyright": re.sub(r"[^0-9]", "", str(
                copyright if copyright not in (None, "") else self.default_copyright)),
            "desc": desc if desc not in (None, "") else self.default_desc,
            "source": source,
        }
        ts = parse_dtime(dtime, self._warn)
        if ts:
            edit["dtime"] = str(ts)
        resp = requests.post(
            "https://api.bilibili.com/x/vu/client/edit",
            headers=headers, data=edit, timeout=30, verify=self.verify)
        data = resp.json()
        if data.get("code") != 0:
            raise Exception(f"封面更新失败：{data}")
        self._log(f"封面已更新：aid={aid} url={cover_url}")
        return True

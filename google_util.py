"""dapang 专用的 Google 表格适配层。

通用取数能力已抽到 gsheet_source.GoogleSheetSource；本文件只负责注入 dapang 的
业务参数（表 ID、去 emoji、YouTube vid 提取、Gist 去重），并保持既有函数签名不变，
供 upload.py 直接调用。
"""
import re

import util
from gsheet_source import GoogleSheetSource

SHEET_ID = "1u-f9CEMoxQoTK6Beix_4YlmtczlnFJWeQab326LABy0"


def _yt_vid(url):
    """从视频链接提取 YouTube vid，兼容 /watch?v=xxx 与完整 URL。"""
    match = re.search(r'[?&]v=([a-zA-Z0-9_-]+)', url)
    return match.group(1) if match else url.replace("/watch?v=", "")


def _source(google_json, worksheet=0):
    """构造注入 dapang 业务参数的表格数据源（worksheet 可为 tab 名称或位置索引）。"""
    return GoogleSheetSource(google_json, SHEET_ID, worksheet=worksheet,
                             title_transform=util.clean, log=util.log)


def get_video_list_from_google(google_json, uploaded=None, worksheet=0, uid=None):
    """取首个「上传状态」为空的待处理行，返回 upload.py 约定的 detail 结构。

    worksheet：要读取的 tab（名称或索引）；uid：当前账号 UID，用于去重 key 按账号隔离。
    读取即认领（写占位「处理中」）；Gist 已记录的回填历史状态并跳过（双保险去重）。
    """
    uploaded = uploaded or {}

    def _dedup_key(vid):
        return f"{uid}:{vid}" if uid else vid

    def _is_done(video_url):
        vid = _yt_vid(video_url)
        key = _dedup_key(vid)
        if key not in uploaded:
            return None
        try:
            return uploaded[key]["ret"]["data"].get("bvid", "") or True
        except Exception:
            return True

    task = _source(google_json, worksheet).next_pending(is_done=_is_done)
    if not task:
        return None
    vid = _yt_vid(task["video_url"])
    detail = {
        "vid": vid,
        "title": task["title"],
        "origin": f"https://www.youtube.com/watch?v={vid}",
        "cover_url": task["cover_url"],
        "override": task["override"],
        "_row": task["row"],
        "_status_col": task["status_col"],
        "_dedup_key": _dedup_key(vid),
    }
    util.log(f"解析到待处理视频：第{task['row']}行 vid={vid}, title={task['title']}")
    return detail


def mark_row_status(google_json, row, col, text, worksheet=0):
    """回写输入表指定行的上传状态列（上传成功/失败后调用）。"""
    _source(google_json, worksheet).mark(row, col, text)

"""Google 表格数据源能力（按“上传状态列”规则取待处理行）。

零业务依赖，仅依赖 gspread / oauth2client；可被其他项目单独拷走复用：
    from gsheet_source import GoogleSheetSource
    src = GoogleSheetSource(creds_dict, sheet_id)
    task = src.next_pending(is_done=lambda url: url in done_set)
    ...
    src.mark_success(task["row"], task["status_col"], bvid="BVxxxx")

设计约定：
- 表ID / 表索引 / 列名映射 / override 列 / 状态列别名，全部可配。
- “读取即认领”：选中行先写占位「处理中 时间」，避免任务中途崩溃重跑导致重复处理。
- 去重与业务字段解析（如 YouTube vid、外部已上传记录）不进本模块，由调用方通过
  is_done 回调注入，保持通用。
"""
import logging
from datetime import datetime

import gspread
from oauth2client.service_account import ServiceAccountCredentials

_LOGGER = logging.getLogger(__name__)

# 默认列名映射：待处理行的三个基础字段。
DEFAULT_COLUMNS = {"title": "标题", "video_url": "视频链接", "cover_url": "缩略图"}

# 上传状态列别名：读取时写占位「处理中」、成功写「时间 bvid」、失败写「失败 时间」。
DEFAULT_STATUS_ALIASES = ("上传状态", "是否上传过", "是否已处理")
DEFAULT_STATUS_HEADER = "上传状态"

# 可选投稿字段列名映射：表格中若存在这些列（支持中/英文别名），则以表格值为准；
# 缺失时由调用方回退到自身默认值。键为内部统一字段名，值为可识别的表头别名列表。
DEFAULT_OVERRIDE_COLUMNS = {
    "title": ["B站标题", "投稿标题", "bili_title"],
    "tid": ["tid", "分区", "分区id"],
    "tags": ["标签", "tags", "tag"],
    "copyright": ["copyright", "版权", "稿件类型"],
    "source": ["来源", "source", "转载来源"],
    "desc": ["简介", "描述", "desc", "简介desc"],
    "dtime": ["定时发布", "dtime", "发布时间"],
    "account": ["账号", "account", "up主"],
}


def _make_log(log):
    """统一日志入口：外部注入单参 log(msg) 则用之，否则回退标准库 logging。"""
    if log is not None:
        return log

    def _default(msg):
        _LOGGER.info(msg)

    return _default


def _find_col(header, aliases):
    """在表头行中按别名列表查找列索引，找不到返回 None。"""
    for name in aliases:
        if name in header:
            return header.index(name)
    return None


class GoogleSheetSource:
    """按“上传状态列”规则从一个工作表取待处理行、认领并回写状态。"""

    def __init__(self, creds_dict, sheet_id, *, worksheet=0,
                 columns=None,
                 status_aliases=DEFAULT_STATUS_ALIASES,
                 status_header=DEFAULT_STATUS_HEADER,
                 override_columns=None,
                 title_transform=None,
                 log=None):
        self.creds_dict = creds_dict
        self.sheet_id = sheet_id
        self.worksheet = worksheet
        self.columns = columns or DEFAULT_COLUMNS
        self.status_aliases = list(status_aliases)
        self.status_header = status_header
        self.override_columns = override_columns or DEFAULT_OVERRIDE_COLUMNS
        self.title_transform = title_transform
        self._log = _make_log(log)
        self._ws = None

    def _open_ws(self):
        """认证并打开目标工作表（缓存到实例）。"""
        if self._ws is not None:
            return self._ws
        self._log("正在认证 Google Sheets...")
        creds = ServiceAccountCredentials.from_json_keyfile_dict(self.creds_dict)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(self.sheet_id)
        self._log(f"Google Sheets 连接成功：{sheet.title}")
        # worksheet 为字符串按 tab 名称打开，为数字按位置索引打开
        if isinstance(self.worksheet, str):
            self._ws = sheet.worksheet(self.worksheet)
        else:
            self._ws = sheet.get_worksheet(self.worksheet)
        return self._ws

    def _ensure_status_col(self, ws, header):
        """确保存在上传状态列，返回其 0 基列索引；缺失时在表头末尾自动补一列。"""
        idx = _find_col(header, self.status_aliases)
        if idx is not None:
            return idx
        idx = len(header)
        ws.update_cell(1, idx + 1, self.status_header)
        self._log(f"输入表缺少状态列，已自动新增：{self.status_header}（第{idx + 1}列）")
        return idx

    def next_pending(self, is_done=None):
        """取首个「上传状态」为空的待处理行。

        读取即认领：先写占位「处理中 时间」。
        is_done(video_url) 返回真值表示该行已在别处处理过：回填「已上传(历史)」并跳过；
        若返回的是字符串（如 bvid），会一并写入状态，便于回溯。
        返回 {video_url, title, cover_url, override, row, status_col} 或 None。
        """
        ws = self._open_ws()
        self._log(f"读取输入表：{ws.title}")
        rows = ws.get_all_values()
        if len(rows) < 2:
            self._log("输入表仅有表头，无待处理视频")
            return None
        header = rows[0]
        title_index = header.index(self.columns["title"])
        video_index = header.index(self.columns["video_url"])
        img_index = header.index(self.columns["cover_url"])
        status_index = self._ensure_status_col(ws, header)

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        for r in range(2, len(rows) + 1):
            row = rows[r - 1]
            status = row[status_index].strip() if status_index < len(row) else ""
            if status:
                continue
            video_url = row[video_index]
            # 去重双保险：调用方判定该行是否已在别处处理过
            if is_done is not None:
                done = is_done(video_url)
                if done:
                    suffix = done if isinstance(done, str) else ""
                    ws.update_cell(r, status_index + 1, f"已上传(历史) {suffix}".strip())
                    self._log(f"第{r}行 已在外部记录，回填状态并跳过")
                    continue
            title_val = row[title_index]
            if self.title_transform:
                title_val = self.title_transform(title_val)
            # 采集可选投稿字段：表格中存在且非空的列以表格为准
            override = {}
            for field, aliases in self.override_columns.items():
                idx = _find_col(header, aliases)
                if idx is not None and idx < len(row):
                    val = row[idx].strip()
                    if val:
                        override[field] = val
            task = {
                "video_url": video_url,
                "title": title_val,
                "cover_url": row[img_index],
                "override": override,
                "row": r,
                "status_col": status_index + 1,
            }
            if override:
                self._log(f"表格覆盖字段：{list(override.keys())}")
            # 读取即认领：写占位，防止崩溃重跑重复处理
            ws.update_cell(r, status_index + 1, f"处理中 {now}")
            self._log(f"解析到待处理行：第{r}行 title={title_val}")
            return task
        self._log("输入表无待处理视频（均已标记）")
        return None

    def mark(self, row, col, text):
        """回写指定行的状态列。"""
        ws = self._open_ws()
        ws.update_cell(row, col, text)
        self._log(f"已回写状态：第{row}行 -> {text}")

    def mark_success(self, row, col, bvid=""):
        """标记成功：写「时间 bvid」。"""
        self.mark(row, col, f'{datetime.now().strftime("%Y-%m-%d %H:%M")} {bvid}'.strip())

    def mark_failed(self, row, col):
        """标记失败：写「失败 时间」。"""
        self.mark(row, col, f'失败 {datetime.now().strftime("%Y-%m-%d %H:%M")}')

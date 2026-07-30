import re
from datetime import datetime

import util
import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_ID = "1u-f9CEMoxQoTK6Beix_4YlmtczlnFJWeQab326LABy0"

# 上传状态列：读取时写占位「处理中」、成功写「时间 bvid」、失败写「失败 时间」。
# 支持中文别名，缺失时自动在表头末尾补一列。
STATUS_ALIASES = ["上传状态", "是否上传过", "是否已处理"]
STATUS_HEADER = "上传状态"

# 可选投稿字段列名映射：表格中若存在这些列（支持中/英文别名），则以表格值为准；
# 缺失时由 upload.py 回退到 config.json 默认值或代码内置值。
# 键为内部统一字段名，值为可识别的表头别名列表。
OVERRIDE_COLUMNS = {
    "title": ["B站标题", "投稿标题", "bili_title"],
    "tid": ["tid", "分区", "分区id"],
    "tags": ["标签", "tags", "tag"],
    "copyright": ["copyright", "版权", "稿件类型"],
    "source": ["来源", "source", "转载来源"],
    "desc": ["简介", "描述", "desc", "简介desc"],
    "dtime": ["定时发布", "dtime", "发布时间"],
    "account": ["账号", "account", "up主"],
}


def _find_col(header, aliases):
    """在表头行中按别名列表查找列索引，找不到返回 None"""
    for name in aliases:
        if name in header:
            return header.index(name)
    return None


'''
从gist上拉取Google表格密钥->完成Google认证->获取远端表格
json做临时缓存，认证后删除
'''


def get_sheet_from_google(google_json):
    # Google表格密钥
    # Google认证
    # 服务器认证
    util.log("正在认证 Google Sheets...")
    creds = ServiceAccountCredentials.from_json_keyfile_dict(google_json)
    # 本地认证
    # creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
    client = gspread.authorize(creds)
    # 通过 ID 打开指定的 Google Sheets 文件
    sheet = client.open_by_key(SHEET_ID)
    util.log(f"Google Sheets 连接成功：{sheet.title}")
    return sheet


def _ensure_status_col(in_sheet, header):
    """确保输入表存在上传状态列，返回其 0 基列索引；缺失时在表头末尾自动补一列。"""
    idx = _find_col(header, STATUS_ALIASES)
    if idx is not None:
        return idx
    idx = len(header)
    in_sheet.update_cell(1, idx + 1, STATUS_HEADER)
    util.log(f"输入表缺少状态列，已自动新增：{STATUS_HEADER}（第{idx + 1}列）")
    return idx


def get_video_list_from_google(google_json, uploaded=None):
    """从输入表(工作表1)取首个「上传状态」为空的待处理行。
    读取即认领：先写占位「处理中」，避免任务中途崩溃重跑导致重复上传。
    行内 vid 若已在 Gist 记录，则回填历史状态并跳过（双保险去重）。"""
    uploaded = uploaded or {}
    # 通过 ID 打开指定的 Google Sheets 文件
    wb = get_sheet_from_google(google_json)
    in_sheet = wb.get_worksheet(0)
    util.log(f"读取输入表：{in_sheet.title}")
    rows = in_sheet.get_all_values()
    if len(rows) < 2:
        # 表格只有表头，无待处理数据
        util.log("输入表仅有表头，无待处理视频")
        return None
    # 获取第一行数据,确认索引位置
    tab_row = rows[0]
    title_index = tab_row.index("标题")
    video_index = tab_row.index("视频链接")
    img_index = tab_row.index("缩略图")
    status_index = _ensure_status_col(in_sheet, tab_row)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    # 逐行查找首个状态列为空的待处理行
    for r in range(2, len(rows) + 1):
        row = rows[r - 1]
        status = row[status_index].strip() if status_index < len(row) else ""
        if status:
            continue
        video_url = row[video_index]
        # 兼容 /watch?v=xxx 和 https://www.youtube.com/watch?v=xxx 两种格式
        match = re.search(r'[?&]v=([a-zA-Z0-9_-]+)', video_url)
        vid = match.group(1) if match else video_url.replace("/watch?v=", "")
        # Gist 去重双保险：历史已上传的 vid 直接回填状态并跳过
        if vid in uploaded:
            bv = ""
            try:
                bv = uploaded[vid]["ret"]["data"].get("bvid", "")
            except Exception:
                pass
            in_sheet.update_cell(r, status_index + 1, (f"已上传(历史) {bv}").strip())
            util.log(f"第{r}行 vid={vid} 已在Gist记录，回填状态并跳过")
            continue
        detail = {
            "vid": vid,
            "title": util.clean(row[title_index]),
            "origin": f"https://www.youtube.com/watch?v={vid}",
            "cover_url": row[img_index],
            "_row": r,
            "_status_col": status_index + 1,
        }
        # 采集可选投稿字段：表格中存在且非空的列以表格为准，随视频一并带出
        override = {}
        for field, aliases in OVERRIDE_COLUMNS.items():
            idx = _find_col(tab_row, aliases)
            if idx is not None and idx < len(row):
                val = row[idx].strip()
                if val:
                    override[field] = val
        detail["override"] = override
        if override:
            util.log(f"表格覆盖字段：{list(override.keys())}")
        # 读取即认领：写占位，防止崩溃重跑重复上传
        in_sheet.update_cell(r, status_index + 1, f"处理中 {now}")
        util.log(f"解析到待处理视频：第{r}行 vid={vid}, title={detail['title']}")
        # 使用数据上传
        return detail
    util.log("输入表无待处理视频（均已标记）")
    return None


def mark_row_status(google_json, row, col, text):
    """回写输入表指定行的上传状态列（上传成功/失败后调用）。"""
    wb = get_sheet_from_google(google_json)
    in_sheet = wb.get_worksheet(0)
    in_sheet.update_cell(row, col, text)
    util.log(f"已回写状态：第{row}行 -> {text}")

# if __name__ == '__main__':
#     get_video_list_from_google()

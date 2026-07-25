import re

import util
import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_ID = "1u-f9CEMoxQoTK6Beix_4YlmtczlnFJWeQab326LABy0"

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


def get_video_list_from_google(google_json):
    # 通过 ID 打开指定的 Google Sheets 文件
    wb = get_sheet_from_google(google_json)
    in_sheet = wb.get_worksheet(0)
    out_sheet = wb.get_worksheet(1)
    util.log(f"读取输入表：{in_sheet.title}，输出表：{out_sheet.title}")

    # 读取整张表格的前2行内容
    rows = in_sheet.get_all_values()[:2]
    util.log(f"输入表行数：{len(rows)}（含表头）")
    # 获取第一行数据,确认索引位置
    tab_row = rows[0]
    title_index = tab_row.index("标题")
    video_index = tab_row.index("视频链接")
    img_index = tab_row.index("缩略图")
    # 构建所需对象
    if len(rows) < 2:
        # 表格只有表头，无待处理数据
        util.log("输入表仅有表头，无待处理视频")
        return None
    row = rows[1]
    video_url = row[video_index]
    # 兼容 /watch?v=xxx 和 https://www.youtube.com/watch?v=xxx 两种格式
    match = re.search(r'[?&]v=([a-zA-Z0-9_-]+)', video_url)
    vid = match.group(1) if match else video_url.replace("/watch?v=", "")
    detail = {
        "vid": vid,
        "title": util.clean(row[title_index]),
        "origin": f"https://www.youtube.com/watch?v={vid}",
        "cover_url": row[img_index],
    }
    util.log(f"解析到视频：vid={vid}, title={detail['title']}")
    # 数据移动到已完成表格
    out_sheet.append_row(row)
    in_sheet.delete_rows(2)
    util.log("视频数据已从输入表移至输出表")
    # 使用数据上传
    return detail

# if __name__ == '__main__':
#     get_video_list_from_google()


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
    creds = ServiceAccountCredentials.from_json_keyfile_dict(google_json)
    # 本地认证
    # creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
    client = gspread.authorize(creds)
    # 通过 ID 打开指定的 Google Sheets 文件
    return client.open_by_key(SHEET_ID)


def get_video_list_from_google(google_json):
    # 通过 ID 打开指定的 Google Sheets 文件
    wb = get_sheet_from_google(google_json)
    in_sheet = wb.get_worksheet(0)
    out_sheet = wb.get_worksheet(1)

    # 读取整张表格的前2行内容
    rows = in_sheet.get_all_values()[:2]
    # 获取第一行数据,确认索引位置
    tab_row = rows[0]
    title_index = tab_row.index("标题")
    video_index = tab_row.index("视频链接")
    video_time_index = tab_row.index("视频时常")
    img_index = tab_row.index("缩略图")
    # 构建所需对象
    row = rows[1]
    detail = {
        "vid": row[video_index].replace("/watch?v=", ""),
        "title": util.clean(row[title_index]),
        "origin": "https://www.youtube.com" + row[video_index],
        "cover_url": row[img_index],
    }
    # 数据移动到已完成表格
    out_sheet.append_row(row)
    in_sheet.delete_rows(2)
    # 使用数据上传
    return detail

# if __name__ == '__main__':
#     get_video_list_from_google()

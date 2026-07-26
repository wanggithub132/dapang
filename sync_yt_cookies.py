"""从本地浏览器提取 YouTube cookies 并同步到 Gist。

用法：
  python sync_yt_cookies.py <github_token> <gist_id>

流程：
  1. 终止 Chrome 进程（释放 cookie 数据库锁）
  2. yt-dlp --cookies-from-browser chrome 提取 cookies
  3. 上传 yt_cookies.txt 到 Gist

建议配合 Windows 任务计划程序定时运行（每周一次）。
"""

import argparse
import logging
import os
import subprocess
import sys
import requests


def log(msg):
    print(msg)
    logging.info(msg)


def log_warn(msg):
    print(f"[WARNING] {msg}")
    logging.warning(msg)


def log_error(msg):
    print(f"[ERROR] {msg}")
    logging.error(msg)


def kill_chrome():
    """强制终止 Chrome 进程（Windows），释放 cookie 数据库锁"""
    if os.name != "nt":
        log("非 Windows 系统，跳过终止 Chrome")
        return
    try:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"],
                       capture_output=True, timeout=10)
        log("Chrome 进程已终止")
    except subprocess.TimeoutExpired:
        log_warn("终止 Chrome 超时")
    except FileNotFoundError:
        log_warn("taskkill 不可用，跳过终止 Chrome")


def extract_cookies():
    """用 yt-dlp 从 Chrome 提取 cookies，保存到 yt_cookies.txt"""
    cookie_file = "yt_cookies.txt"
    cmd = [shutil_which_ytdlp(), "--cookies-from-browser", "chrome",
           "--cookies", cookie_file]
    log(f"执行：{' '.join(cmd)}")
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=60)
    except subprocess.CalledProcessError as e:
        msg = e.output.decode("utf8", errors="replace")
        if "Cookie" in msg and "not found" in msg:
            log("Chrome 中未找到 YouTube cookies，请先登录 YouTube")
        elif "chromium" in msg.lower() or "database is locked" in msg.lower():
            log_error("Chrome cookie 数据库被锁定，请确保 Chrome 已完全关闭")
        else:
            log_error(f"提取失败：{msg}")
        return None
    if not os.path.isfile(cookie_file):
        log_error(f"未生成 {cookie_file}")
        return None
    size = os.path.getsize(cookie_file)
    log(f"已提取 cookies ({size} bytes)")
    return cookie_file


def shutil_which_ytdlp():
    """查找 yt-dlp 路径"""
    # 优先取 Python 同目录下的 yt-dlp.exe
    py_dir = os.path.dirname(sys.executable)
    local = os.path.join(py_dir, "yt-dlp.exe")
    if os.path.isfile(local):
        return local
    import shutil
    return shutil.which("yt-dlp") or "yt-dlp"


def update_gist_text(_gid, token, filename, content):
    """更新 Gist 中的文本文件（非 JSON）"""
    log(f"正在更新 Gist：{filename}")
    rsp = requests.patch(
        "https://api.github.com/gists/" + _gid,
        json={
            "files": {
                filename: {"content": content}
            }
        },
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + token,
        },
    )
    if rsp.status_code == 404:
        raise Exception("gist id 错误")
    if rsp.status_code in (401, 403):
        raise Exception("github TOKEN 无效或权限不足")
    log(f"Gist 更新成功，HTTP {rsp.status_code}")
    return rsp


def main():
    parser = argparse.ArgumentParser(
        description="从本地浏览器提取 YouTube cookies 并同步到 Gist")
    parser.add_argument("token", help="GitHub API Token", type=str)
    parser.add_argument("gistId", help="Gist ID", type=str)
    args = parser.parse_args()

    logging.basicConfig(
        stream=sys.stdout,
        level=logging.INFO,
        format='%(asctime)s.%(msecs)03d %(levelname)s: %(message)s',
        datefmt="%H:%M:%S",
    )

    log("========== YouTube cookies 同步开始 ==========")

    # 1. 终止 Chrome
    kill_chrome()

    # 2. 提取 cookies
    cookie_file = extract_cookies()
    if not cookie_file:
        log_error("提取失败，终止")
        sys.exit(1)

    # 3. 上传到 Gist
    with open(cookie_file, "r", encoding="utf8") as f:
        content = f.read()
    update_gist_text(args.gistId, args.token, "yt_cookies.txt", content)

    # 4. 清理本地临时文件
    os.remove(cookie_file)
    log("临时文件已清理")

    log("========== YouTube cookies 同步完成 ==========")


if __name__ == "__main__":
    main()

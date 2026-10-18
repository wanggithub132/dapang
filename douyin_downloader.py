"""抖音博主作品列表 + 无水印视频/封面下载能力（patchright 浏览器 + 直链 HTTP 下载）。

零业务依赖（公共工具复用 util；patchright 为可选依赖，仅抓列表时导入），可被其他项目单独拷走复用：
    from douyin_downloader import DouyinDownloader
    dl = DouyinDownloader(storage_state="cookies/douyin_account2.json")
    posts = dl.list_posts("https://www.douyin.com/user/MS4w...", limit=20)
    dl.download(posts[0], "123.mp4")
    dl.download_cover(posts[0]["cover_url"], "123.jpg")

原理：抖音主页作品列表走 /aweme/v1/web/aweme/post/ 接口，必须带浏览器生成的风控签名
（a_bogus/msToken），纯 HTTP 请求会被 ArgusSecurityPlugin 拦成 403。这里用 patchright
打开博主主页并拦截该接口响应，拿到 aweme_list；视频直链取 video.play_addr（web 播放地址，
无水印），用浏览器同源 cookie + UA 下载，封面同理。

注意：抖音无头浏览器会被反爬（页面只剩空壳、作品接口不触发），CI 请用 xvfb-run 跑有头模式。
"""
import os
import re
import time
import logging

import requests

from util import ensure_jpeg

_LOGGER = logging.getLogger(__name__)

# 主页链接里的 sec_uid；也允许直接传 sec_uid 本身
_SEC_UID_RE = re.compile(r"/user/([A-Za-z0-9_\-]+)")
# 单次抓取最多下载的直链候选数（bit_rate 里有很多清晰度，逐个试太慢）
_MAX_URL_CANDIDATES = 6


def _make_log(log):
    """统一日志入口：外部注入单参 log(msg) 则用之，否则回退标准库 logging。"""
    if log is not None:
        return log

    def _default(msg):
        _LOGGER.info(msg)

    return _default


class DouyinDownloader:
    """抓取抖音博主作品列表并下载无水印视频/封面。"""

    def __init__(self, storage_state=None, *, channel=None, headless=False,
                 page_timeout=60000, first_batch_timeout=30000,
                 download_timeout=600, verify=True, proxy=None,
                 min_bytes=100 * 1024, log=None):
        """storage_state：patchright 的登录态文件（sau 的 douyin_<账号>.json）。
        headless：抖音无头会被反爬，默认有头；CI 用 xvfb-run 提供虚拟显示。
        channel：浏览器通道，如 "chrome"；None 用 patchright 自带 chromium。
        proxy：None=不显式设置（requests 仍会读环境变量）；非空=显式代理。
        """
        self.storage_state = storage_state
        self.channel = channel
        self.headless = headless
        self.page_timeout = page_timeout
        self.first_batch_timeout = first_batch_timeout
        self.download_timeout = download_timeout
        self.verify = verify
        self.proxy = proxy
        self.min_bytes = min_bytes
        self._log = _make_log(log)
        # 抓列表时从浏览器上下文带出来，供后续 HTTP 下载复用
        self._ua = None
        self._cookie_header = ""

    def _warn(self, msg):
        self._log(f"[WARN] {msg}")

    def _debug(self, msg):
        self._log(f"[DEBUG] {msg}")

    @staticmethod
    def extract_sec_uid(user):
        """从主页链接提取 sec_uid，兼容直接传 sec_uid。"""
        user = (user or "").strip()
        if not user:
            raise ValueError("缺少抖音博主主页链接或 sec_uid")
        m = _SEC_UID_RE.search(user)
        return m.group(1) if m else user

    def _launch_kwargs(self):
        kwargs = {
            "headless": self.headless,
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }
        if self.channel:
            kwargs["channel"] = self.channel
        if self.proxy:
            kwargs["proxy"] = {"server": self.proxy}
        return kwargs

    def _headers(self):
        headers = {
            "User-Agent": self._ua or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            "Referer": "https://www.douyin.com/",
        }
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        return headers

    def _proxies(self):
        """显式代理时给 requests 也用同一个（None 保持环境变量默认行为）。"""
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def list_posts(self, user, limit=20, pages=1):
        """打开博主主页，返回按发布时间倒序的作品列表（最多 limit 条）。

        pages>1 时会向下滚动加载更多页；每条作品结构：
        {aweme_id, desc, url, create_time, duration_s, is_image, cover_url, video_urls}
        """
        try:
            from patchright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "缺少 patchright：pip install patchright 并执行 patchright install chromium") from e

        sec_uid = self.extract_sec_uid(user)
        profile_url = f"https://www.douyin.com/user/{sec_uid}"
        batches = []

        def on_response(resp):
            if "/aweme/v1/web/aweme/post/" not in resp.url:
                return
            try:
                body = resp.json()
            except Exception:
                return
            if isinstance(body, dict) and "aweme_list" in body:
                batches.append(body)
                self._debug(f"捕获作品接口响应：{len(body.get('aweme_list') or [])} 条，"
                            f"has_more={body.get('has_more')}")

        self._log(f"打开抖音主页：{profile_url}")
        with sync_playwright() as p:
            browser = p.chromium.launch(**self._launch_kwargs())
            try:
                ctx_kwargs = {"viewport": {"width": 1440, "height": 900}}
                if self.storage_state and os.path.isfile(self.storage_state):
                    ctx_kwargs["storage_state"] = self.storage_state
                else:
                    self._warn(f"未找到抖音登录态：{self.storage_state}，可能只返回部分作品或撞登录墙")
                ctx = browser.new_context(**ctx_kwargs)
                page = ctx.new_page()
                page.on("response", on_response)
                page.goto(profile_url, wait_until="domcontentloaded",
                          timeout=self.page_timeout)
                # 等首个作品接口响应（接口触发时间受页面渲染/风控影响，给足窗口）
                deadline = time.time() + self.first_batch_timeout / 1000
                while time.time() < deadline and not batches:
                    page.wait_for_timeout(500)
                # 滚动加载后续页
                for _ in range(max(0, pages - 1)):
                    last = batches[-1] if batches else {}
                    if not last.get("has_more"):
                        break
                    before = len(batches)
                    scroll_deadline = time.time() + 8
                    while len(batches) == before and time.time() < scroll_deadline:
                        page.mouse.move(720, 450)
                        page.mouse.wheel(0, 3000)
                        page.wait_for_timeout(500)
                if not batches:
                    raise RuntimeError(
                        "未捕获抖音作品接口（常见原因：无头浏览器被反爬/登录态失效/IP 被风控）："
                        f"url={page.url}, title={page.title()!r}")
                # 直链下载需要与浏览器同源的 cookie + UA
                self._ua = page.evaluate("() => navigator.userAgent")
                self._cookie_header = "; ".join(
                    f'{c["name"]}={c["value"]}' for c in ctx.cookies()
                    if "douyin.com" in (c.get("domain") or ""))
            finally:
                browser.close()

        posts, seen = [], set()
        for batch in batches:
            for aweme in batch.get("aweme_list") or []:
                post = self._normalize(aweme)
                if post["aweme_id"] in seen:
                    continue
                seen.add(post["aweme_id"])
                posts.append(post)
        posts.sort(key=lambda x: x["create_time"], reverse=True)
        self._log(f"博主 {sec_uid} 获取到 {len(posts)} 条作品"
                  f"（视频 {sum(1 for x in posts if not x['is_image'])} 条，"
                  f"图文 {sum(1 for x in posts if x['is_image'])} 条）")
        return posts[:limit]

    @staticmethod
    def _normalize(aweme):
        """把抖音原始 aweme 结构归一化为下载需要的字段。"""
        video = aweme.get("video") or {}
        images = aweme.get("images") or []
        urls = []
        # 优先 H.264 1080p（B站转码兼容性最好），其次 web 默认播放地址，
        # 最后用 bit_rate 里的其余清晰度兜底
        urls += (video.get("play_addr_h264") or {}).get("url_list") or []
        urls += (video.get("play_addr") or {}).get("url_list") or []
        for br in sorted(video.get("bit_rate") or [],
                         key=lambda x: x.get("bit_rate") or 0, reverse=True):
            urls += (br.get("play_addr") or {}).get("url_list") or []
        video_urls = []
        for u in urls:
            if u and u not in video_urls:
                video_urls.append(u)
        cover_url = ""
        for key in ("origin_cover", "cover", "raw_cover"):
            for u in (video.get(key) or {}).get("url_list") or []:
                if u:
                    cover_url = u
                    break
            if cover_url:
                break
        duration_ms = video.get("duration") or aweme.get("duration") or 0
        return {
            "aweme_id": str(aweme.get("aweme_id") or ""),
            "desc": aweme.get("desc") or aweme.get("caption") or aweme.get("preview_title") or "",
            "url": f"https://www.douyin.com/video/{aweme.get('aweme_id')}",
            "create_time": int(aweme.get("create_time") or 0),
            "duration_s": round(int(duration_ms) / 1000, 1),
            "is_image": bool(images),
            "cover_url": cover_url,
            "video_urls": [] if images else video_urls,
        }

    def download(self, post, out, retries=1):
        """下载作品视频到 out；多直链依次尝试，全部失败抛异常。"""
        if post.get("is_image"):
            raise ValueError(f"作品 {post.get('aweme_id')} 是图文，不是视频")
        urls = (post.get("video_urls") or [])[:_MAX_URL_CANDIDATES]
        if not urls:
            raise ValueError(f"作品 {post.get('aweme_id')} 无可用视频直链")
        last_error = None
        for url in urls:
            for _ in range(max(1, retries)):
                try:
                    size = self._stream_to(url, out)
                    if size < self.min_bytes:
                        last_error = f"文件过小({size} bytes)"
                        continue
                    self._log(f"视频下载完成：{out}（{size / 1024 / 1024:.1f} MB）")
                    return out
                except Exception as e:
                    last_error = str(e)
                    self._warn(f"直链下载失败（{last_error}），换下一个直链")
        raise RuntimeError(f"视频下载失败：{last_error}")

    def _stream_to(self, url, out):
        """流式下载单个直链，先写 .part 再原子替换，避免残片被误当成品。"""
        tmp = out + ".part"
        try:
            with requests.get(url, headers=self._headers(), stream=True,
                              timeout=(15, self.download_timeout),
                              proxies=self._proxies(),
                              verify=self.verify) as resp:
                if resp.status_code not in (200, 206):
                    raise RuntimeError(f"HTTP {resp.status_code}")
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype or "application/json" in ctype:
                    raise RuntimeError(f"返回非视频内容：{ctype}")
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(1024 * 1024):
                        if chunk:
                            f.write(chunk)
        except Exception:
            for candidate in (tmp, out):
                try:
                    os.remove(candidate)
                except OSError:
                    pass
            raise
        size = os.path.getsize(tmp) if os.path.isfile(tmp) else 0
        if size < self.min_bytes:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise RuntimeError(f"文件过小({size} bytes)")
        os.replace(tmp, out)
        return size

    def download_cover(self, url, out):
        """下载封面并统一转 JPEG（B站图床只认 jpeg/png/gif）。"""
        if not url:
            return False
        self._debug(f"下载封面：{url[:120]}")
        resp = requests.get(url, headers=self._headers(), timeout=30,
                            proxies=self._proxies(), verify=self.verify)
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"封面下载失败：HTTP {resp.status_code}")
        with open(out, "wb") as f:
            f.write(resp.content)
        ensure_jpeg(out, self._warn)
        self._log(f"封面下载完成：{out}（{len(resp.content)} bytes）")
        return True

    def cleanup(self, *paths):
        """删除下载产生的临时文件。"""
        for path in paths:
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

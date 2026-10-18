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

注意：抖音无头浏览器会被反爬（页面只剩空壳、作品接口不触发），CI 请用有头模式。
浏览器的 locale/timezone 必须显式设为 zh-CN/Asia/Shanghai：默认 en-US + UTC 在抖音
风控里就是机器人特征，作品接口会直接返回 403 Sign Invalid（参考 douyin-auto-fire）。
"""
import json
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

    # Argus 网关对开放平台来源豁免签名校验：请求头带 Origin: open.douyin.com 时，
    # 作品列表接口不需要 a_bogus 也能返回 200（实测 1.5MB 完整数据）。
    # 来源：luojiahai/skills#121（yt-dlp 给 open 平台 Referer/Origin 绕过 Argus）
    _ARGUS_ORIGIN = "https://open.douyin.com"
    _DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")

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

    def _log_browser_env(self, page):
        """打印关键指纹，失败时用于区分 locale/GPU/UA 哪一层被风控。"""
        try:
            info = page.evaluate("""() => {
                const gl = document.createElement('canvas').getContext('webgl');
                const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
                return {
                    ua: navigator.userAgent,
                    lang: navigator.language,
                    langs: (navigator.languages || []).join(','),
                    tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    webgl: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)
                               : (gl ? 'webgl(no-debug-ext)' : 'no-webgl'),
                    cores: navigator.hardwareConcurrency,
                    mem: navigator.deviceMemory,
                };
            }""")
            self._debug(f"浏览器环境：{info}")
        except Exception as e:
            self._debug(f"浏览器环境探测失败（忽略）：{e}")

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
            # --lang 让浏览器 UI/ACCEPT-LANGUAGE 与页面 locale 一致，避免中英混杂被判定异常
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled",
                     "--lang=zh-CN"],
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

    def _load_cookies(self):
        """从 storage_state 读 douyin.com 的 cookie，返回 (dict, cookie 头字符串)。"""
        if not self.storage_state or not os.path.isfile(self.storage_state):
            return {}, ""
        try:
            with open(self.storage_state, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}, ""
        cookies = {}
        for c in state.get("cookies") or []:
            if "douyin.com" in (c.get("domain") or "") and c.get("name"):
                cookies[c["name"]] = c.get("value") or ""
        header = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return cookies, header

    def _list_posts_http(self, sec_uid, limit, pages):
        """纯 HTTP 拉作品列表：curl_cffi 模拟 Chrome TLS 指纹 + 开放平台 Origin 头。"""
        from curl_cffi import requests as creq

        cookies, cookie_header = self._load_cookies()
        if not cookies:
            raise RuntimeError(f"未找到可用登录态：{self.storage_state}")
        ua = self._ua or self._DEFAULT_UA
        headers = {
            "User-Agent": ua,
            "Referer": "https://www.douyin.com/",
            "Origin": self._ARGUS_ORIGIN,
            "Accept": "application/json, text/plain, */*",
        }
        posts, seen, cursor = [], set(), "0"
        for page in range(max(1, pages)):
            params = {
                "device_platform": "webapp", "aid": "6383",
                "channel": "channel_pc_web", "sec_user_id": sec_uid,
                "max_cursor": str(cursor), "count": "20",
                "publish_video_strategy_type": "2",
                "pc_client_type": "1", "pc_libra_divert": "Windows",
                "version_code": "170400", "version_name": "17.4.0",
                "cookie_enabled": "true", "screen_width": "1440",
                "screen_height": "900", "browser_language": "zh-CN",
                "browser_platform": "Win32", "browser_name": "Chrome",
                "browser_version": "145.0.0.0", "browser_online": "true",
                "engine_name": "Blink", "engine_version": "145.0.0.0",
                "os_name": "Windows", "os_version": "10",
                "cpu_core_num": "8", "device_memory": "8", "platform": "PC",
                "downlink": "10", "effective_type": "4g", "round_trip_time": "50",
            }
            r = creq.get("https://www.douyin.com/aweme/v1/web/aweme/post/",
                         params=params, headers=headers, cookies=cookies,
                         impersonate="chrome", timeout=30,
                         proxies=self._proxies(), verify=self.verify)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}：{(r.text or '')[:120]}")
            try:
                body = r.json()
            except ValueError:
                raise RuntimeError(f"非 JSON 响应：{(r.text or '')[:120]}")
            aweme_list = body.get("aweme_list")
            if not isinstance(aweme_list, list):
                raise RuntimeError(f"响应缺 aweme_list（status_code={body.get('status_code')}）")
            for aweme in aweme_list:
                post = self._normalize(aweme)
                if post["aweme_id"] and post["aweme_id"] not in seen:
                    seen.add(post["aweme_id"])
                    posts.append(post)
            self._debug(f"作品列表（HTTP 第 {page + 1} 页）：{len(aweme_list)} 条，"
                        f"has_more={body.get('has_more')}")
            if len(posts) >= limit or not body.get("has_more"):
                break
            cursor = body.get("max_cursor") or 0
            if not cursor:
                break
        posts.sort(key=lambda x: x["create_time"], reverse=True)
        self._ua = ua
        self._cookie_header = cookie_header
        self._log(f"博主 {sec_uid} 获取到 {len(posts)} 条作品"
                  f"（视频 {sum(1 for x in posts if not x['is_image'])} 条，"
                  f"图文 {sum(1 for x in posts if x['is_image'])} 条）")
        return posts[:limit]

    def list_posts(self, user, limit=20, pages=1):
        """获取博主作品列表，返回按发布时间倒序的作品列表（最多 limit 条）。

        优先纯 HTTP：请求头带 Origin: open.douyin.com 让 Argus 网关豁免签名校验，
        不依赖浏览器，数据中心 IP（CI）也能跑；失败再回退到浏览器拦截作品接口。

        每条作品结构：
        {aweme_id, desc, url, create_time, duration_s, is_image, cover_url, video_urls}
        """
        sec_uid = self.extract_sec_uid(user)
        errors = []

        try:
            posts = self._list_posts_http(sec_uid, limit, pages)
            if posts:
                return posts
            errors.append("纯 HTTP 返回 0 条作品")
        except Exception as e:
            errors.append(f"纯 HTTP：{e}")
        self._warn(f"纯 HTTP 抓取失败（{errors[-1]}），回退浏览器模式")

        try:
            import patchright.sync_api  # noqa: F401  仅用于提前给出安装提示
        except ImportError as e:
            raise RuntimeError("；".join(errors) + "；且缺少 patchright 兜底") from e

        profile_url = f"https://www.douyin.com/user/{sec_uid}"

        # 带登录态失败时自动匿名重试：数据中心 IP（如 Azure）上，本地导出的 cookie
        # 其 ttwid 与出口 IP 不匹配，容易被判为会话异常，作品接口直接 403
        attempts = [True]
        if self.storage_state and os.path.isfile(self.storage_state):
            attempts.append(False)
        errors = []
        for idx, use_cookie in enumerate(attempts):
            try:
                batches = self._scrape(profile_url, use_cookie, pages)
            except Exception as e:
                errors.append(str(e))
                if idx + 1 < len(attempts):
                    self._warn(f"抓取失败：{e}")
                    self._warn("改用匿名模式重试（先访问首页建立会话）")
                continue
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
        raise RuntimeError("；".join(errors))

    def _scrape(self, profile_url, use_cookie, pages):
        """单次抓取，返回作品接口的原始响应批次；失败抛异常。"""
        from patchright.sync_api import sync_playwright

        batches = []
        post_status = {"code": None}

        def on_response(resp):
            if "/aweme/v1/web/" not in resp.url:
                return
            if "/aweme/v1/web/aweme/post/" in resp.url:
                post_status["code"] = resp.status
                body = None
                try:
                    body = resp.json()
                except Exception:
                    body = None
                if isinstance(body, dict):
                    lst = body.get("aweme_list") or []
                    # 200 但没有 aweme_list 通常是「需要登录/风控降级」，打出业务状态码才能区分
                    self._debug(f"作品列表接口：HTTP {resp.status} "
                                f"status_code={body.get('status_code')} list={len(lst)} "
                                f"msg={str(body.get('status_msg'))[:40]}")
                    if "aweme_list" in body:
                        batches.append(body)
                        self._debug(f"捕获作品接口响应：{len(lst)} 条，"
                                    f"has_more={body.get('has_more')}")
                else:
                    snippet = ""
                    try:
                        snippet = (resp.text() or "")[:200].replace("\n", " ")
                    except Exception:
                        pass
                    self._debug(f"作品列表接口：HTTP {resp.status} 非 JSON：{snippet}")
            elif "/aweme/v1/web/user/profile/" in resp.url:
                self._debug(f"用户资料接口：HTTP {resp.status}")

        self._log(f"打开抖音主页：{profile_url}" + ("" if use_cookie else "（匿名模式）"))
        with sync_playwright() as p:
            browser = p.chromium.launch(**self._launch_kwargs())
            try:
                ctx_kwargs = {
                    "viewport": {"width": 1440, "height": 900},
                    # 抖音按 locale/时区判定真人：默认 en-US+UTC 会签出无效 a_bogus
                    "locale": "zh-CN",
                    "timezone_id": "Asia/Shanghai",
                    "device_scale_factor": 1,
                    "is_mobile": False,
                    "has_touch": False,
                }
                if use_cookie and self.storage_state and os.path.isfile(self.storage_state):
                    ctx_kwargs["storage_state"] = self.storage_state
                elif use_cookie:
                    self._warn(f"未找到抖音登录态：{self.storage_state}，可能只返回部分作品或撞登录墙")
                ctx = browser.new_context(**ctx_kwargs)
                page = ctx.new_page()
                page.on("response", on_response)
                if not use_cookie:
                    # 先访问首页，让 ttwid/msToken 等会话 cookie 在出口 IP 下自然生成
                    try:
                        page.goto("https://www.douyin.com/", wait_until="domcontentloaded",
                                  timeout=self.page_timeout)
                        page.wait_for_timeout(2000)
                    except Exception as e:
                        self._debug(f"首页预热失败（忽略）：{e}")
                page.goto(profile_url, wait_until="domcontentloaded",
                          timeout=self.page_timeout)
                self._log_browser_env(page)
                # 等首个作品接口响应。作品区可能在折叠线下方，必须滚动才会触发懒加载，
                # 干等会一直不触发
                deadline = time.time() + self.first_batch_timeout / 1000
                next_scroll = time.time() + 3
                while time.time() < deadline and not batches:
                    if time.time() >= next_scroll:
                        page.mouse.move(720, 450)
                        page.mouse.wheel(0, 1200)
                        next_scroll = time.time() + 3
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
                    # 页面文本能区分「登录墙」和「纯懒加载未触发」：前者出现「登录后查看」等字样
                    page_text = ""
                    try:
                        page_text = (page.inner_text("body") or "").strip().replace("\n", " ")[:300]
                    except Exception:
                        pass
                    try:
                        page.screenshot(path="douyin_fail.png")
                        self._warn("抓取失败截图：douyin_fail.png")
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"作品接口未返回数据（HTTP {post_status['code']}；"
                        "常见原因：IP 被风控/登录态失效/懒加载未触发）："
                        f"title={page.title()!r}, 页面文本={page_text!r}")
                # 直链下载需要与浏览器同源的 cookie + UA
                self._ua = page.evaluate("() => navigator.userAgent")
                self._cookie_header = "; ".join(
                    f'{c["name"]}={c["value"]}' for c in ctx.cookies()
                    if "douyin.com" in (c.get("domain") or ""))
            finally:
                browser.close()
        return batches

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

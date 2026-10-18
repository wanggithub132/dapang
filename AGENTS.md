# AGENTS.md

This file provides guidance to Qoder (qoder.com) when working with code in this repository.

## Project Overview

This is a Python automation tool that downloads YouTube videos (via `yt-dlp`) and re-uploads them to Bilibili (via `biliup-rs`). Upload state is persisted in GitHub Gists, and additional video sources can be pulled from a Google Sheet.

It also supports the reverse-shaped flow: following a **Douyin creator** and syncing their new posts to Bilibili (`douyin_sync.py`), using patchright to capture the Douyin profile's `aweme/post` API.

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the upload process (requires GitHub token + Gist ID)
python upload.py <github_token> <gist_id>

# Run with debug logging
python upload.py <github_token> <gist_id> --logLevel DEBUG

# Sync a Douyin creator's new posts to Bilibili (config: douyin_channels.json)
python douyin_sync.py <github_token> <gist_id>

# Local dry run: list what would be synced, no download/upload/state write
python douyin_sync.py --dry-run --channels douyin_channels.json
```

**Prerequisites:**
- `biliup` CLI must be installed separately from [biliup-rs](https://github.com/ForgQi/biliup-rs/releases)
- `biliup login` must be run once locally to generate `cookies.json`
- Douyin sync needs `patchright` + Chromium: `patchright install chromium`（CI 用 `xvfb-run` 跑有头浏览器）
- Environment variable `https_proxy` is used for proxy; `verify=0` disables SSL verification

## Architecture

### Data Flow

1. **Config & State from Gist** — `upload.py:get_gist()` fetches 4 JSON files from a GitHub Gist: `config.json` (channel list + upload settings), `cookie.json` (Bilibili session), `uploaded_video.json` (already-uploaded video IDs), `google_credentials.json` (Google Sheets service account key)
2. **Video Discovery** — `upload.py:get_all_video()` iterates over configured YouTube channels, fetching their RSS feeds (`/feeds/videos.xml?channel_id=...`) and parsing with `xmltodict`. It also pulls one video entry from a Google Sheet via `google_util.py`
3. **Filtering** — `upload.py:select_not_uploaded()` removes already-uploaded videos (tracked by `vid` in `uploaded_video.json`) and applies channel-specific rules
4. **Download → Upload Loop** — For each pending video:
   - `download_video()` calls `yt-dlp` subprocess, trying formats `webm → flv → mp4` in order
   - `download_cover()` fetches the thumbnail via HTTP
   - `upload_video()` generates a `config.yaml` and calls `biliup upload` subprocess
   - Temp files are deleted after upload
5. **State Sync** — After each successful upload, `uploaded_video.json` is updated in the Gist. After all uploads, cookies are renewed via `biliup renew` and synced back

### Douyin → Bilibili Sync

1. **Config** — `douyin_channels.json`（Gist 同名文件优先）列出博主：`user`/`sec_uid`、`douyin_account`、`tid`、`tags`、`copyright`、`max_per_run`
2. **Discovery** — `douyin_downloader.py` 用 patchright 打开 `https://www.douyin.com/user/<sec_uid>`，拦截 `/aweme/v1/web/aweme/post/` 响应拿到 `aweme_list`（纯 HTTP 会被 `ArgusSecurityPlugin` 403；无头浏览器接口不触发，必须 xvfb 有头）
3. **Filtering** — 首次运行只写基线（不推历史）；之后按 `create_time` 升序补推新视频，图文作品记为跳过
4. **Download** — 取 `video.play_addr`（web 播放地址，无水印）多直链回退流式下载；封面取 `origin_cover` 转 JPEG
5. **Upload** — 复用 `BilibiliUploader`；标题取抖音文案首个非空行（去 emoji/`#话题`），标签 = `#话题` + 配置固定标签
6. **State** — 每成功一条立即写回 Gist `douyin_synced.json`；失败不记状态，下次重试

### Key Modules

| File | Purpose |
|---|---|
| `upload.py` | Main entry point and orchestrator. Handles Gist I/O, YouTube RSS parsing, download/upload pipeline |
| `douyin_sync.py` | 抖音博主 → B站 同步入口：Gist 状态、基线/增量筛选、标题标签清洗、投稿编排 |
| `douyin_downloader.py` | 抖音作品列表抓取（patchright 拦截 aweme/post）+ 无水印视频/封面下载，零业务依赖 |
| `douyin_channels.json` | 抖音博主页配置（示例见 `douyin_channels.example.json`） |
| `google_util.py` | Reads video metadata from a Google Sheet (hardcoded sheet ID `SHEET_ID`). Moves processed rows to a "completed" worksheet |
| `util.py` | Shared helpers: `clean()` emoji stripping, `cli_path`, `ensure_jpeg`, logging |
| `config.yaml` | Generated at runtime by `upload_video()` for biliup CLI consumption (not the same as Gist's `config.json`) |

### Gist as Database

The GitHub Gist acts as the persistent store for all state. Four JSON files live in the Gist:
- `config.json` — array of channel configs with `channel_id`, `tid` (Bilibili category), `tags`
- `cookie.json` — Bilibili login cookies
- `uploaded_video.json` — map of `vid → upload record`
- `google_credentials.json` — Google service account key for Sheets access
- `douyin_synced.json` — 抖音同步进度：`sec_uid → {baseline_at, seen:{aweme_id: {bvid,...}}}`
- `douyin_channels.json`（可选）— 覆盖仓库里的博主配置，便于不改代码调整
- `douyin_cookie_<账号>.json` — patchright 抖音登录态（sau login 生成的 storage_state）

### Hardcoded Values to Be Aware Of

- `SHEET_ID` in `google_util.py` — Google Sheet ID is hardcoded
- `vid == "5LT8Y_bgozs"` skip rule and `channel_id == "UC9h7Az08limpxBK7ycxS-SA"` filter in `select_not_uploaded()`
- `UPLOAD_SLEEP_SECOND = 120` — 2-minute sleep between uploads to avoid captcha
- 抖音同步：`DOUYIN_SYNC_SLEEP` 默认 120 秒、`max_per_run` 默认 3、`copyright` 默认 2（转载）
- Bilibili upload config (copyright, desc, tid, etc.) is hardcoded in `upload_video()`

## GitHub Actions

| Workflow | File | Trigger | Description |
|---|---|---|---|
| Upload | `.github/workflows/upload.yaml` | 定时（每日 03:00/10:00/13:00 UTC）、push master、手动 | 安装 ffmpeg + biliup-rs + yt-dlp，执行 `python upload.py` |
| Douyin Sync | `.github/workflows/douyin_sync.yaml` | 每 3 小时、手动 | 安装 xvfb + patchright/chromium，`xvfb-run python douyin_sync.py` |
| Delete | `.github/workflows/delete.yaml` | 每月1日 00:00、手动 | 删除30天前的旧工作流运行记录 |

**Secrets 配置：**
- `secrets.GIST_ID` — Gist ID
- `secrets.GIT_TOKEN` — GitHub API Token

**手动触发：** 支持通过 `workflow_dispatch` 传入 `logLevel`（INFO/DEBUG）。

**环境：** Python 3.12，Ubuntu，时区设为 `Asia/Shanghai`。`yt-dlp` 在 CI 中会被升级到最新版。

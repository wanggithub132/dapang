# AGENTS.md

This file provides guidance to Qoder (qoder.com) when working with code in this repository.

## Project Overview

This is a Python automation tool that downloads YouTube videos (via `yt-dlp`) and re-uploads them to Bilibili (via `biliup-rs`). Upload state is persisted in GitHub Gists, and additional video sources can be pulled from a Google Sheet.

## Common Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the upload process (requires GitHub token + Gist ID)
python upload.py <github_token> <gist_id>

# Run with debug logging
python upload.py <github_token> <gist_id> --logLevel DEBUG
```

**Prerequisites:**
- `biliup` CLI must be installed separately from [biliup-rs](https://github.com/ForgQi/biliup-rs/releases)
- `biliup login` must be run once locally to generate `cookies.json`
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

### Key Modules

| File | Purpose |
|---|---|
| `upload.py` | Main entry point and orchestrator. Handles Gist I/O, YouTube RSS parsing, download/upload pipeline |
| `google_util.py` | Reads video metadata from a Google Sheet (hardcoded sheet ID `SHEET_ID`). Moves processed rows to a "completed" worksheet |
| `util.py` | Single `clean()` function to strip emoji from strings |
| `config.yaml` | Generated at runtime by `upload_video()` for biliup CLI consumption (not the same as Gist's `config.json`) |

### Gist as Database

The GitHub Gist acts as the persistent store for all state. Four JSON files live in the Gist:
- `config.json` — array of channel configs with `channel_id`, `tid` (Bilibili category), `tags`
- `cookie.json` — Bilibili login cookies
- `uploaded_video.json` — map of `vid → upload record`
- `google_credentials.json` — Google service account key for Sheets access

### Hardcoded Values to Be Aware Of

- `SHEET_ID` in `google_util.py` — Google Sheet ID is hardcoded
- `vid == "5LT8Y_bgozs"` skip rule and `channel_id == "UC9h7Az08limpxBK7ycxS-SA"` filter in `select_not_uploaded()`
- `UPLOAD_SLEEP_SECOND = 120` — 2-minute sleep between uploads to avoid captcha
- Bilibili upload config (copyright, desc, tid, etc.) is hardcoded in `upload_video()`

## GitHub Actions

| Workflow | File | Trigger | Description |
|---|---|---|---|
| Upload | `.github/workflows/upload.yaml` | 定时（每日 03:00/10:00/13:00 UTC）、push master、手动 | 安装 ffmpeg + biliup-rs + yt-dlp，执行 `python upload.py` |
| Delete | `.github/workflows/delete.yaml` | 每月1日 00:00、手动 | 删除30天前的旧工作流运行记录 |

**Secrets 配置：**
- `secrets.GIST_ID` — Gist ID
- `secrets.GIT_TOKEN` — GitHub API Token

**手动触发：** 支持通过 `workflow_dispatch` 传入 `logLevel`（INFO/DEBUG）。

**环境：** Python 3.8，Ubuntu，时区设为 `Asia/Shanghai`。`yt-dlp` 在 CI 中会被升级到最新版。

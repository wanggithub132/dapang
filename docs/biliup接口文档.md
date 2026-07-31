# B站投稿能力接口文档（biliup-rs）

> 本文档说明本项目"推送到B站"所依赖的能力、认证方式、命令接口、参数、返回值，以及项目的实际调用方式。

## 一、能力提供方

本项目**推送到B站用的不是B站官方开放 API**，而是第三方开源命令行工具 **biliup-rs**（Rust 实现）。它内部封装了B站 App/Web 端的投稿接口（`member.bilibili.com` 系列），对外暴露为 CLI 命令。

| 项 | 内容 |
|---|---|
| 名称 | biliup-rs |
| 类型 | 命令行工具（非 REST API） |
| 官方仓库 | https://github.com/biliup/biliup-rs （旧地址 ForgQi/biliup-rs） |
| 官方使用文档 | https://biliup.github.io/biliup-rs/Guide.html |
| crates.io | https://crates.io/crates/biliup |
| 项目中调用位置 | `upload.py` → `upload_video()` |

> 说明：B站官方**没有对个人开放**的投稿 API，biliup-rs 是靠模拟官方客户端登录态（cookies）来投稿的，所以它不是"官方 API 文档"，而是工具文档。

## 二、认证方式

biliup-rs 靠**登录态 cookies** 认证，不用 API Key。

| 命令 | 作用 |
|---|---|
| `biliup login` | 首次登录（扫码/短信/密码），生成 `cookies.json` |
| `biliup renew` | 刷新 cookies 有效期，续期后仍写回 `cookies.json` |

本项目做法：从 GitHub Gist 取出 `cookies.json` 写到本地 → 投稿 → 结束后 `biliup renew` 续期 → 再把新 cookies 同步回 Gist（见 `upload.py` → `upload_process()`）。

## 三、核心接口：`biliup upload`

```
biliup upload [OPTIONS] [VIDEO_PATH]...
```

| 参数 | 说明 | 默认值 | 本项目取值 |
|---|---|---|---|
| `<VIDEO_PATH>...` | 视频文件路径，支持多 P（多文件=一个稿件多分P） | — | 单个 mp4 |
| `-l, --line <LINE>` | 上传线路：`kodo` / `bda2` / `qn` / `ws` | 自动 | `ws` |
| `--submit <SUBMIT>` | 提交接口：`app` / `client` / `web`（新版参数，旧版可能无） | — | `app` |
| `--tid <TID>` | 投稿分区 ID | `171` | 来自 Gist 配置 `config.tid`（示例 `71`=美食） |
| `--copyright <N>` | `1`=自制/原创，`2`=转载 | `1` | `1` |
| `--source <SOURCE>` | 转载来源（copyright=2 时需要） | — | 原视频 YouTube 链接 |
| `--title <TITLE>` | 稿件标题 | — | 表格标题（截断至 80 字） |
| `--tag <TAG>` | 标签，多个用逗号分隔 | — | 来自 Gist 配置 `config.tags` |
| `--desc <DESC>` | 简介 | — | `定期更新，喜欢的话求点赞投币关注！` |
| `--cover <COVER>` | 封面图路径 | — | 本项目**未使用**（cover 参数已禁用） |
| `--dtime <DTIME>` | 定时发布（10 位时间戳，需距提交>4h） | — | 未用 |
| `--dynamic <DYNAMIC>` | 空间动态文案 | — | 未用 |
| `--limit <LIMIT>` | 单文件最大并发数 | `3` | 未用 |
| `-c, --config <FILE>` | 用配置文件投稿（yaml），指定后不用传 VIDEO_PATH | — | 未用 |
| `-h, --help` | 帮助 | — | — |

> ⚠️ 版权一致性：本项目 `--copyright 1`（标原创）却同时传 `--source`（转载来源），语义是矛盾的。若走转载合规路线，应改 `--copyright 2` + 保留 `--source`。

## 四、本项目实际调用命令

```bash
biliup upload \
  --line ws \
  --submit app \
  --tid <config.tid> \
  --copyright 1 \
  --title <标题> \
  --tag <config.tags> \
  --source <YouTube链接> \
  --desc "定期更新，喜欢的话求点赞投币关注！" \
  ./<vid>.mp4
```

> `./` 前缀用于规避 `-` 开头的文件名被 CLI 当成选项参数的问题。

## 五、返回值结构

biliup 成功后在 stdout 输出 JSON，本项目取倒数第 2 行解析（`upload.py` → `upload_video()`）：

```json
{
  "code": 0,
  "data": {
    "aid": 123456789,
    "bvid": "BVxxxxxxxxxx"
  }
}
```

| 字段 | 含义 |
|---|---|
| `code` | `0`=成功，非 0=失败 |
| `data.aid` | 稿件 av 号 |
| `data.bvid` | 稿件 BV 号 |

失败时进程返回码非 0，项目抛异常并打印 stderr/stdout。

## 六、其它命令（项目用到 / 相关）

| 命令 | 作用 | 项目是否用 |
|---|---|---|
| `biliup login` | 登录生成 cookies.json | 手动一次性 |
| `biliup renew` | 续期 cookies | ✅ 每轮上传后 |
| `biliup upload` | 投稿 | ✅ 核心 |
| `biliup append` | 向已有稿件追加分 P | ❌ |
| `biliup download` | 下载视频 | ❌ |
| `biliup list` / `show` | 查看稿件 | ❌ |

## 七、配置文件投稿模式（备用，项目未使用）

除命令行参数外，biliup 也支持用 yaml 配置文件批量投稿（`biliup upload -c config.yaml`），可用 Unix shell 通配符匹配多个视频：

```yaml
line: kodo
limit: 3
streamers:
  "视频patterns1*":
    copyright: 1
    source: 转载来源
    tid: 171          # 投稿分区
    cover: ""         # 视频封面
    title: 标题
    desc_format_id: 0
    desc: 简介
    dynamic: ""
    tag: ""
    dtime: ~
```

同一条匹配规则内的视频合并为一个稿件多 P；多个匹配条目分成多个稿件。

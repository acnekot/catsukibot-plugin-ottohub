# CatsukiBot OTTOhub 视频下载插件

为 CatsukiBot v3 提供 [OTTOhub 滚幕网](https://www.ottohub.cn/)视频解析、下载与 OneBot/NapCat 发送功能。

## 功能

- 自动识别群聊和私聊中的 `https://www.ottohub.cn/v/<视频ID>` 链接
- 支持 `#otto <链接或视频ID>` 主动下载
- 群聊默认以合并转发发送“视频信息 + 视频”
- 合并转发失败时自动降级为分开发送
- 大文件自动改用群文件或私聊文件上传
- 支持 JSON 分享卡片链接识别
- 下载大小限制、超时、缓存、并发锁和重复解析抑制
- 可在 CatsukiBot WebUI 中调整发送方式及各项限制

## 环境要求

- Python 3.11+
- CatsukiBot v3
- OneBot v11 / NapCat
- `aiohttp >= 3.9, < 4`

## 安装

在 CatsukiBot 项目根目录执行：

```powershell
git clone https://github.com/acnekot/catsukibot-plugin-ottohub.git .tmp-ottohub-plugin
Copy-Item -Recurse -Force .tmp-ottohub-plugin\ottohub_downloader .\catsukibot\plugins\
```

然后在 `config/config.yaml` 的 `plugins.load` 中加入：

```yaml
plugins:
  load:
    - ottohub_downloader
```

重启 CatsukiBot，或在机器人中执行：

```text
#load ottohub_downloader
```

更新已安装的插件时，重新复制 `ottohub_downloader` 目录后执行：

```text
#reload ottohub_downloader
```

## 使用

直接发送 OttoHub 视频链接，或者使用命令：

```text
#otto https://www.ottohub.cn/v/32253
#otto 32253
```

命令别名：`#ottohub`、`#otto下载`、`#滚幕下载`。

## WebUI 设置

| 设置项 | 默认值 | 说明 |
| --- | --- | --- |
| `auto_parse` | `true` | 自动解析聊天中的 OttoHub 链接 |
| `send_mode` | `auto` | `auto`、`video` 或 `file` |
| `video_send_mode` | `forward` | 群聊使用 `forward` 合并转发或 `separate` 分开发送 |
| `direct_send_max_mb` | `100` | 自动模式下的视频消息大小上限 |
| `max_download_mb` | `500` | 单个视频最大下载体积 |
| `download_timeout_sec` | `600` | 视频下载超时 |
| `cache_ttl_hours` | `6` | 本地缓存时间，设为 `0` 表示不保留缓存 |

超过视频消息上限时，插件会上传群文件或私聊文件；文件上传本身不能嵌入合并转发。

## 开发与测试

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check ottohub_downloader tests
```

## 免责声明

请只下载和转发你有权使用的内容，并遵守 OTTOhub、QQ/NapCat 以及所在地法律法规的相关要求。本项目与 OTTOhub 官方无隶属关系。

## 许可证

[MIT](LICENSE)

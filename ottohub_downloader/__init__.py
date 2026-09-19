"""OTTOhub video downloader for CatsukiBot v3."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
from catsukibot.core.plugin import Plugin

if TYPE_CHECKING:
    from catsukibot.models.context import EventContext


API_BASE = "https://api.ottohub.cn"
SITE_BASE = "https://www.ottohub.cn"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CatsukiBot/3 OTTOhubDownloader/1.0"
VIDEO_URL_RE = re.compile(
    r"https?://(?:www\.)?ottohub\.cn/v/(?P<vid>\d+)(?:[/?#][^\s]*)?",
    re.IGNORECASE,
)
VIDEO_ID_RE = re.compile(r"^\d{1,20}$")
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class OttoHubError(RuntimeError):
    """A user-facing OTTOhub download error."""


def extract_video_id(text: str) -> str | None:
    """Extract a numeric OTTOhub video id from a URL or a command argument."""
    value = (text or "").strip()
    match = VIDEO_URL_RE.search(value)
    if match:
        return match.group("vid")
    if VIDEO_ID_RE.fullmatch(value):
        return value
    return None


def safe_filename(value: str, *, fallback: str = "video", limit: int = 80) -> str:
    """Return a Windows-safe filename stem."""
    name = INVALID_FILENAME_RE.sub("_", value).strip(" .")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = fallback
    if name.upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name[:limit].rstrip(" .") or fallback


def format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def format_duration(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "未知"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def api_call_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    status = str(result.get("status") or "").lower()
    retcode = result.get("retcode")
    return status in {"failed", "error"} or retcode not in (None, 0, 1)


class OttoHubDownloaderPlugin(Plugin):
    id = "ottohub_downloader"
    name = "OTTOhub 视频下载"
    version = "1.1.0"

    def __init__(self, bot):
        super().__init__(bot)
        self._session: aiohttp.ClientSession | None = None
        self._download_sem = asyncio.Semaphore(2)
        self._video_locks: dict[str, asyncio.Lock] = {}
        self._recent: dict[tuple[str, str], float] = {}
        self._cache_dir = Path("data") / "ottohub_videos"

    def get_webui_meta(self) -> dict:
        return {
            "title": self.name,
            "icon": "🎬",
            "description": "自动解析并下载 OTTOhub 视频，支持合并转发和群/私聊文件",
            "show_in_sidebar": False,
            "show_settings": True,
        }

    def get_settings_schema(self) -> list[dict]:
        return [
            {
                "key": "auto_parse",
                "type": "bool",
                "label": "自动解析聊天中的 OTTOhub 链接",
                "default": True,
            },
            {
                "key": "send_mode",
                "type": "select",
                "label": "发送方式",
                "default": "auto",
                "options": ["auto", "video", "file"],
            },
            {
                "key": "video_send_mode",
                "type": "select",
                "label": "群聊视频呈现方式",
                "default": "forward",
                "options": ["forward", "separate"],
            },
            {
                "key": "direct_send_max_mb",
                "type": "int",
                "label": "视频消息大小上限（MB）",
                "default": 100,
                "min": 1,
                "max": 500,
            },
            {
                "key": "max_download_mb",
                "type": "int",
                "label": "允许下载的最大视频（MB）",
                "default": 500,
                "min": 1,
                "max": 4096,
            },
            {
                "key": "download_timeout_sec",
                "type": "int",
                "label": "下载超时（秒）",
                "default": 600,
                "min": 30,
                "max": 3600,
            },
            {
                "key": "cache_ttl_hours",
                "type": "int",
                "label": "本地视频缓存时间（小时）",
                "default": 6,
                "min": 0,
                "max": 168,
            },
        ]

    def get_commands(self) -> list[dict]:
        return [
            {
                "cmd": "otto",
                "aliases": ["ottohub", "otto下载", "滚幕下载"],
                "desc": "下载 OTTOhub 视频：#otto 链接或视频ID",
                "perm": "所有人",
            }
        ]

    async def on_load(self):
        await asyncio.to_thread(self._cache_dir.mkdir, parents=True, exist_ok=True)
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
            headers={"User-Agent": USER_AGENT, "Referer": f"{SITE_BASE}/"}
        )
        self.register_command(
            "otto",
            self.cmd_download,
            aliases=["ottohub", "otto下载", "滚幕下载"],
        )
        self.on_event("message")(self.on_message)
        self.create_task(self._cleanup_cache())
        self.register_interval(3600, self._cleanup_cache)
        self.bot.logger.info("ottohub_downloader.loaded", cache_dir=str(self._cache_dir))

    async def on_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def cmd_download(self, ctx: EventContext):
        video_id = extract_video_id(ctx._cmd_args)
        if not video_id:
            await ctx.reply(
                "用法：#otto https://www.ottohub.cn/v/12345\n"
                "也可以直接发送：#otto 12345"
            )
            return
        await self._process(ctx, video_id, explicit=True)

    async def on_message(self, ctx: EventContext):
        settings = await self._settings()
        if not settings["auto_parse"]:
            return

        text = self._message_search_text(ctx)
        if not text or text.lstrip().startswith(self.bot.config.bot.command_prefix):
            return
        video_id = extract_video_id(text)
        if not video_id:
            return

        if ctx.event.group_id is not None and not (
            await self.bot.permissions.check_access_ctx(ctx, "bot")
        ):
            return
        if not await self.bot.permissions.check_access_ctx(
            ctx, f"plugin.{self.id}", self.default_access
        ):
            return

        recent_key = (self._conversation_key(ctx), video_id)
        now = time.monotonic()
        self._recent = {key: ts for key, ts in self._recent.items() if now - ts < 300}
        if recent_key in self._recent:
            return
        self._recent[recent_key] = now
        success = await self._process(ctx, video_id, explicit=False)
        if not success:
            self._recent.pop(recent_key, None)

    async def _process(self, ctx: EventContext, video_id: str, *, explicit: bool) -> bool:
        if ctx.event.platform != "onebot":
            await ctx.reply("当前 OTTOhub 下载插件仅支持 OneBot/NapCat 会话。")
            return False

        await ctx.set_emoji_like("10024")
        file_path: Path | None = None
        settings: dict[str, Any] | None = None
        try:
            settings = await self._settings()
            detail = await self._fetch_detail(video_id, settings["download_timeout_sec"])
            title = str(detail.get("title") or f"OTTOhub_{video_id}")
            file_path = await self._get_video_file(detail, settings)
            size = file_path.stat().st_size

            info = (
                f"🎬 {title}\n"
                f"UP：{detail.get('username') or '未知'}  "
                f"时长：{format_duration(detail.get('duration'))}  "
                f"大小：{format_size(size)}\n"
                f"链接：{SITE_BASE}/v/{video_id}"
            )
            await self._send_file(ctx, file_path, title, size, settings, info)
            await ctx.set_emoji_like("124")
            self.bot.logger.info(
                "ottohub_downloader.sent",
                vid=video_id,
                size=size,
                explicit=explicit,
            )
            return True
        except OttoHubError as exc:
            await ctx.reply(f"⚠️ OTTOhub 视频下载失败：{exc}")
            self.bot.logger.warning(
                "ottohub_downloader.failed", vid=video_id, error=str(exc)
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await ctx.reply("⚠️ OTTOhub 视频下载失败，请稍后再试。")
            self.bot.logger.exception(
                "ottohub_downloader.error", vid=video_id, error=str(exc)
            )
        finally:
            if file_path is not None and settings and settings["cache_ttl_hours"] == 0:
                try:
                    await asyncio.to_thread(file_path.unlink, missing_ok=True)
                except OSError:
                    self.bot.logger.debug(
                        "ottohub_downloader.cache_cleanup_failed", path=str(file_path)
                    )
        return False

    async def _fetch_detail(self, video_id: str, timeout_seconds: int) -> dict[str, Any]:
        session = self._require_session()
        url = f"{API_BASE}/api/video/{video_id}"
        timeout = aiohttp.ClientTimeout(total=min(timeout_seconds, 60), connect=15)
        try:
            async with session.get(
                url,
                params={"no_history": 1},
                timeout=timeout,
            ) as response:
                if response.status == 404:
                    raise OttoHubError("视频不存在或已被删除")
                if response.status == 429:
                    raise OttoHubError("访问过于频繁，请稍后再试")
                if response.status != 200:
                    raise OttoHubError(f"详情接口返回 HTTP {response.status}")
                try:
                    payload = await response.json(content_type=None)
                except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                    raise OttoHubError("详情接口返回了无效数据") from exc
        except TimeoutError as exc:
            raise OttoHubError("获取视频信息超时") from exc
        except aiohttp.ClientError as exc:
            raise OttoHubError(f"无法连接 OTTOhub：{exc}") from exc

        if not isinstance(payload, dict) or payload.get("status") == "error":
            message = payload.get("message") if isinstance(payload, dict) else None
            raise OttoHubError(str(message or "获取视频信息失败"))
        data = payload.get("data")
        if not isinstance(data, dict) or str(data.get("vid") or "") != video_id:
            raise OttoHubError("视频详情数据不完整")
        if not data.get("video_url") and not data.get("video_m3u8_url"):
            raise OttoHubError("该视频暂无可用下载地址")
        return data

    async def _get_video_file(self, detail: dict[str, Any], settings: dict[str, Any]) -> Path:
        video_id = str(detail["vid"])
        lock = self._video_locks.setdefault(video_id, asyncio.Lock())
        async with lock:
            cached = self._find_cached(video_id, settings["cache_ttl_hours"])
            if cached:
                return cached

            video_url = str(detail.get("video_url") or "").strip()
            if not video_url:
                raise OttoHubError("该视频仅提供 HLS 播放流，暂时无法直接下载")
            self._validate_media_url(video_url)

            title = safe_filename(str(detail.get("title") or "video"))
            final_path = self._cache_dir / f"{video_id}_{title}.mp4"
            part_path = final_path.with_suffix(".mp4.part")
            max_bytes = settings["max_download_mb"] * 1024 * 1024
            timeout_seconds = settings["download_timeout_sec"]

            async with self._download_sem:
                try:
                    await self._download(
                        video_url,
                        part_path,
                        max_bytes=max_bytes,
                        timeout_seconds=timeout_seconds,
                    )
                    await asyncio.to_thread(part_path.replace, final_path)
                except BaseException:
                    await asyncio.to_thread(part_path.unlink, missing_ok=True)
                    raise
            return final_path

    async def _download(
        self,
        url: str,
        part_path: Path,
        *,
        max_bytes: int,
        timeout_seconds: int,
    ) -> None:
        session = self._require_session()
        timeout = aiohttp.ClientTimeout(
            total=timeout_seconds,
            connect=20,
            sock_read=60,
        )
        try:
            async with session.get(url, timeout=timeout) as response:
                # OTTOhub's current object CDN returns a full-body 206 response
                # even when the client did not send a Range header.
                if response.status not in {200, 206}:
                    raise OttoHubError(f"视频源返回 HTTP {response.status}")
                self._validate_media_url(str(response.url))
                declared = response.content_length
                if declared is not None and declared > max_bytes:
                    raise OttoHubError(
                        f"视频大小 {format_size(declared)}，超过 "
                        f"{format_size(max_bytes)} 的下载上限"
                    )

                downloaded = 0
                with part_path.open("wb") as output:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise OttoHubError(
                                f"视频超过 {format_size(max_bytes)} 的下载上限"
                            )
                        output.write(chunk)
                if downloaded == 0:
                    raise OttoHubError("视频源返回了空文件")
        except TimeoutError as exc:
            raise OttoHubError("视频下载超时") from exc
        except aiohttp.ClientError as exc:
            raise OttoHubError(f"视频下载连接失败：{exc}") from exc

    async def _send_file(
        self,
        ctx: EventContext,
        path: Path,
        title: str,
        size: int,
        settings: dict[str, Any],
        info: str,
    ) -> None:
        mode = settings["send_mode"]
        direct_limit = settings["direct_send_max_mb"] * 1024 * 1024
        try_video = mode == "video" or (mode == "auto" and size <= direct_limit)
        info_sent = False

        if try_video:
            is_group = bool(
                ctx.event.message_type
                and ctx.event.message_type.value == "group"
                and ctx.event.group_id is not None
            )
            if is_group and settings["video_send_mode"] == "forward":
                try:
                    await self._send_forward_video(ctx, path, info)
                    return
                except Exception as exc:
                    self.bot.logger.warning(
                        "ottohub_downloader.forward_send_failed",
                        error=str(exc),
                        fallback="separate",
                    )

            await ctx.reply(info)
            info_sent = True
            result = await self._send_video_message(ctx, path)
            if not api_call_failed(result):
                return
            self.bot.logger.warning(
                "ottohub_downloader.video_send_failed",
                result=str(result),
                fallback="file",
            )

        if not info_sent:
            await ctx.reply(info)
        filename = f"{safe_filename(title)}_{path.name.split('_', 1)[0]}.mp4"
        result = await self._upload_file(ctx, path, filename)
        if api_call_failed(result):
            detail = result.get("wording") or result.get("message") or result.get("msg")
            raise OttoHubError(f"文件发送失败：{detail or 'OneBot 接口调用失败'}")

    async def _send_forward_video(
        self,
        ctx: EventContext,
        path: Path,
        info: str,
    ) -> None:
        resolved = await asyncio.to_thread(path.resolve)
        user_id = str(ctx.event.self_id or "0")
        nickname = "OTTOhub 视频下载"
        await ctx.send_forward_msg(
            [
                {
                    "user_id": user_id,
                    "nickname": nickname,
                    "content": [{"type": "text", "data": {"text": info}}],
                },
                {
                    "user_id": user_id,
                    "nickname": nickname,
                    "content": [
                        {"type": "video", "data": {"file": str(resolved)}}
                    ],
                },
            ]
        )

    async def _send_video_message(self, ctx: EventContext, path: Path) -> dict:
        resolved = await asyncio.to_thread(path.resolve)
        message = [{"type": "video", "data": {"file": str(resolved)}}]
        if ctx.event.message_type and ctx.event.message_type.value == "group":
            return await ctx.call_api(
                "send_group_msg",
                group_id=ctx.event.group_id,
                message=message,
            )
        return await ctx.call_api(
            "send_private_msg",
            user_id=ctx.event.user_id,
            message=message,
        )

    async def _upload_file(self, ctx: EventContext, path: Path, filename: str) -> dict:
        resolved = await asyncio.to_thread(path.resolve)
        if ctx.event.message_type and ctx.event.message_type.value == "group":
            return await ctx.call_api(
                "upload_group_file",
                group_id=ctx.event.group_id,
                file=str(resolved),
                name=filename,
            )
        return await ctx.call_api(
            "upload_private_file",
            user_id=ctx.event.user_id,
            file=str(resolved),
            name=filename,
        )

    async def _settings(self) -> dict[str, Any]:
        raw = await self.get_settings()

        def as_int(key: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(raw.get(key, default))
            except (TypeError, ValueError):
                value = default
            return min(max(value, minimum), maximum)

        mode = str(raw.get("send_mode", "auto"))
        if mode not in {"auto", "video", "file"}:
            mode = "auto"
        video_send_mode = str(raw.get("video_send_mode", "forward"))
        if video_send_mode not in {"forward", "separate"}:
            video_send_mode = "forward"
        return {
            "auto_parse": bool(raw.get("auto_parse", True)),
            "send_mode": mode,
            "video_send_mode": video_send_mode,
            "direct_send_max_mb": as_int("direct_send_max_mb", 100, 1, 500),
            "max_download_mb": as_int("max_download_mb", 500, 1, 4096),
            "download_timeout_sec": as_int("download_timeout_sec", 600, 30, 3600),
            "cache_ttl_hours": as_int("cache_ttl_hours", 6, 0, 168),
        }

    def _find_cached(self, video_id: str, ttl_hours: int) -> Path | None:
        now = time.time()
        for path in self._cache_dir.glob(f"{video_id}_*.mp4"):
            try:
                age = now - path.stat().st_mtime
                if ttl_hours > 0 and age <= ttl_hours * 3600 and path.stat().st_size > 0:
                    return path
            except OSError:
                continue
        return None

    async def _cleanup_cache(self) -> None:
        settings = await self._settings()
        ttl_hours = settings["cache_ttl_hours"]
        cutoff = time.time() - ttl_hours * 3600
        try:
            paths = list(self._cache_dir.iterdir())
        except OSError:
            return
        for path in paths:
            try:
                if path.suffix == ".part" or ttl_hours == 0 or path.stat().st_mtime < cutoff:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError:
                self.bot.logger.debug(
                    "ottohub_downloader.cache_cleanup_failed", path=str(path)
                )

    @staticmethod
    def _validate_media_url(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not (
            host == "ottohub.cn" or host.endswith(".ottohub.cn")
        ):
            raise OttoHubError("视频源地址不受信任")

    @staticmethod
    def _message_search_text(ctx: EventContext) -> str:
        parts = [ctx.event.plain_text, str(ctx.event.raw.get("raw_message") or "")]

        def collect(value: Any, depth: int = 0) -> None:
            if depth > 4:
                return
            if isinstance(value, str):
                parts.append(value)
                if value.lstrip().startswith(("{", "[")):
                    try:
                        collect(json.loads(value), depth + 1)
                    except (json.JSONDecodeError, TypeError):
                        pass
            elif isinstance(value, dict):
                for nested in value.values():
                    collect(nested, depth + 1)
            elif isinstance(value, list):
                for nested in value:
                    collect(nested, depth + 1)

        for segment in ctx.event.message or []:
            if not isinstance(segment, dict):
                continue
            collect(segment.get("data"))
        return "\n".join(parts)

    @staticmethod
    def _conversation_key(ctx: EventContext) -> str:
        if ctx.event.group_id is not None:
            return f"group:{ctx.event.group_id}"
        return f"private:{ctx.event.user_id}"

    def _require_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise OttoHubError("下载会话尚未就绪")
        return self._session

import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

# The production plugin imports CatsukiBot's Plugin base. A tiny stub keeps the
# standalone repository's unit tests runnable without checking out CatsukiBot.
plugin_module = ModuleType("catsukibot.core.plugin")


class Plugin:
    def __init__(self, bot: Any):
        self.bot = bot

    async def get_settings(self) -> dict:
        return {}

    def register_command(self, *_args, **_kwargs):
        return None

    def on_event(self, *_args, **_kwargs) -> Callable:
        return lambda function: function

    def create_task(self, *_args, **_kwargs):
        return None

    def register_interval(self, *_args, **_kwargs):
        return None


plugin_module.Plugin = Plugin
sys.modules.setdefault("catsukibot", ModuleType("catsukibot"))
sys.modules.setdefault("catsukibot.core", ModuleType("catsukibot.core"))
sys.modules.setdefault("catsukibot.core.plugin", plugin_module)

from ottohub_downloader import (  # noqa: E402
    OttoHubDownloaderPlugin,
    OttoHubError,
    api_call_failed,
    extract_video_id,
    format_duration,
    safe_filename,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://www.ottohub.cn/v/32253", "32253"),
        ("看看 https://ottohub.cn/v/42?from=share 啊", "42"),
        ("32253", "32253"),
        ("https://www.ottohub.cn/", None),
        ("https://evil.example/v/32253", None),
    ],
)
def test_extract_video_id(value, expected):
    assert extract_video_id(value) == expected


def test_safe_filename_handles_windows_names_and_characters():
    assert safe_filename('bad<name>:"/\\|?*') == "bad_name________"
    assert safe_filename("CON") == "_CON"
    assert safe_filename(" ... ") == "video"


def test_format_duration():
    assert format_duration(65) == "1:05"
    assert format_duration(3661) == "1:01:01"
    assert format_duration(None) == "未知"


def test_api_call_failed():
    assert not api_call_failed({"status": "ok", "retcode": 0})
    assert api_call_failed({"status": "failed", "retcode": 100})
    assert api_call_failed(None)


def test_media_url_validation():
    OttoHubDownloaderPlugin._validate_media_url(
        "https://oss-ocdn-1-qecdn.ottohub.cn/video/example"
    )
    with pytest.raises(OttoHubError):
        OttoHubDownloaderPlugin._validate_media_url("http://127.0.0.1/video.mp4")


def test_video_cache_lookup(tmp_path: Path):
    plugin = object.__new__(OttoHubDownloaderPlugin)
    plugin._cache_dir = tmp_path
    cached = tmp_path / "12_title.mp4"
    cached.write_bytes(b"video")
    assert plugin._find_cached("12", 1) == cached
    assert plugin._find_cached("12", 0) is None


def test_message_search_text_reads_json_cards():
    card = json.dumps({"meta": {"detail": {"url": "https://www.ottohub.cn/v/88"}}})
    ctx = SimpleNamespace(
        event=SimpleNamespace(
            plain_text="",
            raw={},
            message=[{"type": "json", "data": {"data": card}}],
        )
    )
    text = OttoHubDownloaderPlugin._message_search_text(ctx)
    assert extract_video_id(text) == "88"


@pytest.mark.asyncio
async def test_download_accepts_full_body_206_and_checks_declared_size(tmp_path: Path):
    class Response:
        status = 206
        url = "https://media.ottohub.cn/video/example.mp4"
        content_length = 2 * 1024 * 1024

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class Session:
        closed = False

        def get(self, *_args, **_kwargs):
            return Response()

    plugin = object.__new__(OttoHubDownloaderPlugin)
    plugin._session = Session()
    with pytest.raises(OttoHubError, match="下载上限"):
        await plugin._download(
            "https://media.ottohub.cn/video/example.mp4",
            tmp_path / "video.part",
            max_bytes=1024 * 1024,
            timeout_seconds=30,
        )


def make_send_plugin():
    plugin = object.__new__(OttoHubDownloaderPlugin)
    plugin.bot = SimpleNamespace(
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None)
    )
    return plugin


def send_settings(**overrides):
    values = {
        "send_mode": "auto",
        "video_send_mode": "forward",
        "direct_send_max_mb": 100,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_group_video_uses_forward_message_by_default(tmp_path: Path):
    path = tmp_path / "88_title.mp4"
    path.write_bytes(b"video")

    class Context:
        event = SimpleNamespace(
            message_type=SimpleNamespace(value="group"),
            group_id=123,
            self_id=456,
        )
        nodes = None

        async def send_forward_msg(self, nodes):
            self.nodes = nodes

        async def reply(self, _message):
            raise AssertionError("合并转发成功时不应分开发送信息")

    ctx = Context()
    await make_send_plugin()._send_file(
        ctx,
        path,
        "标题",
        path.stat().st_size,
        send_settings(),
        "视频信息",
    )
    assert len(ctx.nodes) == 2
    assert ctx.nodes[0]["content"][0]["data"]["text"] == "视频信息"
    assert ctx.nodes[1]["content"][0]["type"] == "video"
    assert ctx.nodes[1]["content"][0]["data"]["file"] == str(path.resolve())


@pytest.mark.asyncio
async def test_forward_failure_falls_back_to_separate_video(tmp_path: Path):
    path = tmp_path / "88_title.mp4"
    path.write_bytes(b"video")

    class Context:
        event = SimpleNamespace(
            message_type=SimpleNamespace(value="group"),
            group_id=123,
            self_id=456,
        )

        def __init__(self):
            self.replies = []
            self.calls = []

        async def send_forward_msg(self, _nodes):
            raise RuntimeError("forward unavailable")

        async def reply(self, message):
            self.replies.append(message)

        async def call_api(self, action, **params):
            self.calls.append((action, params))
            return {"status": "ok", "retcode": 0}

    ctx = Context()
    await make_send_plugin()._send_file(
        ctx,
        path,
        "标题",
        path.stat().st_size,
        send_settings(),
        "视频信息",
    )
    assert ctx.replies == ["视频信息"]
    assert ctx.calls[0][0] == "send_group_msg"
    assert ctx.calls[0][1]["message"][0]["type"] == "video"

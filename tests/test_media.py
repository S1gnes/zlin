"""Скачивание медиа: лимиты Bot API, потоковое видео, уборка."""
import httpx
import pytest

from zlinbot.media import MAX_GROUP, PHOTO_LIMIT, VIDEO_LIMIT, MediaStore

JPEG = b"\xff\xd8\xff" + b"x" * 1000


def photo(url="https://zlin.cz/img/a.jpg"):
    return {"kind": "photo", "url": url}


def store(tmp_path, handler) -> MediaStore:
    return MediaStore(tmp_path / "media", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})


async def test_downloads_and_names_files_with_kind(tmp_path):
    async with store(tmp_path, ok_handler) as s:
        report = await s.fetch(7, [photo(), {"kind": "video", "url": "https://x/v.mp4"}])
    assert [m.kind for m in report.files] == ["photo", "video"]
    assert report.files[0].size == len(JPEG) and report.files[0].path.exists()
    assert [m.kind for m in s.local(7)] == ["photo", "video"]     # вид восстанавливается из имени
    assert report.skipped == ()


async def test_photo_over_limit_is_skipped_by_content_length(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x", headers={"Content-Length": str(PHOTO_LIMIT + 1)})

    async with store(tmp_path, handler) as s:
        report = await s.fetch(1, [photo()])
    assert report.files == () and "больше лимита Bot API" in report.skipped[0]
    assert s.files(1) == []                                        # огрызок не остался на диске


async def test_limit_enforced_even_without_content_length(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (PHOTO_LIMIT + 10))  # сервер не сказал размер

    async with store(tmp_path, handler) as s:
        report = await s.fetch(1, [photo()])
    assert report.files == () and report.skipped


async def test_video_limit_is_larger_than_photo_limit():
    assert (PHOTO_LIMIT, VIDEO_LIMIT) == (10 * 1024 * 1024, 50 * 1024 * 1024)


@pytest.mark.parametrize("url", ["https://video.fb/playlist.m3u8", "https://video.fb/manifest.mpd?x=1"])
async def test_streaming_video_is_skipped_not_stitched(tmp_path, url):
    async with store(tmp_path, ok_handler) as s:
        report = await s.fetch(1, [{"kind": "video", "url": url}])
    assert report.files == () and "HLS/DASH" in report.skipped[0]


async def test_no_more_than_ten_files_in_a_group(tmp_path):
    async with store(tmp_path, ok_handler) as s:
        report = await s.fetch(1, [photo(f"https://zlin.cz/img/{i}.jpg") for i in range(13)])
    assert len(report.files) == MAX_GROUP == 10
    assert "сверх 10 файлов" in report.skipped[0]


async def test_download_failure_does_not_break_the_rest(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("bad.jpg"):
            return httpx.Response(404)
        return ok_handler(request)

    async with store(tmp_path, handler) as s:
        report = await s.fetch(1, [photo("https://zlin.cz/img/bad.jpg"), photo("https://zlin.cz/img/good.jpg")])
    assert len(report.files) == 1 and "не скачалось" in report.skipped[0]


async def test_clear_and_purge(tmp_path):
    async with store(tmp_path, ok_handler) as s:
        await s.fetch(1, [photo()])
        await s.fetch(2, [photo()])
        assert s.clear(1) == 1 and s.files(1) == []
        assert s.purge_older_than(0, now=10 ** 12) == 1            # второй черновик «завис» — подмели
        assert s.files(2) == []

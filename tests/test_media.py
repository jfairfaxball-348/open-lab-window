"""Passive media parsing/ranges preserve the Window's read-only boundary."""

import hashlib
import io
import struct
import wave
from pathlib import Path

import pytest

from conftest import run_url
from open_lab_window.media import MAX_BYTES, media_type


def wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 22050, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00" * 2205)
    return output.getvalue()


def element(ident, raw):
    length = len(raw)
    size = bytes([0x80 | length]) if length < 127 else (0x4000 | length).to_bytes(2, "big")
    return ident + size + raw


def webm(codec=b"V_VP8", width=128, duration=2000.0):
    header = element(bytes.fromhex("1a45dfa3"), element(bytes.fromhex("4282"), b"webm"))
    info = element(bytes.fromhex("1549a966"),
                   element(bytes.fromhex("4489"), struct.pack(">d", duration)))
    video = element(b"\xe0", element(b"\xb0", width.to_bytes(2, "big"))
                    + element(b"\xba", b"\x60"))
    entry = element(b"\xae", element(b"\x83", b"\x01")
                    + element(b"\x86", codec) + video)
    tracks = element(bytes.fromhex("1654ae6b"), entry)
    cluster = element(bytes.fromhex("1f43b675"), b"\xe7\x81\x00")
    return header + element(bytes.fromhex("18538067"), info + tracks + cluster)


def test_only_bounded_pcm_and_supported_webm_are_recognised():
    for data, mime in [(wav(), "audio/wav"), (webm(), "video/webm")]:
        assert media_type(data, len(data)) == mime
        assert media_type(data, MAX_BYTES + 1) is None
        assert media_type(data[:10], len(data)) is None
    for data in (b"<svg><script>bad()</script></svg>", b"RIFF0000WAVEgarbage",
                 webm(codec=b"V_UNSUPPORTED"), webm(width=4096), webm(duration=float("nan")),
                 webm(duration=601000), webm()[:-1]):
        assert media_type(data, len(data)) is None


def test_pcm_structure_and_limits_are_checked():
    original = wav()
    for offset, raw in [(20, b"\x03\x00"), (22, b"\x10\x00"), (24, b"\xff" * 4),
                        (32, b"\xff\xff"), (40, b"\xff" * 4)]:
        data = original[:offset] + raw + original[offset + len(raw):]
        assert media_type(data, len(data)) is None


@pytest.mark.parametrize("kind,data,mime", [("audio", wav(), "audio/wav"),
                                           ("video", webm(), "video/webm")])
def test_preview_stream_ranges_head_and_methods(client, writer, kind, data, mime):
    archive = writer()
    archive.start()
    ident = archive.artifact("untrusted-name.html", data, media_type="text/html")
    base = run_url((archive.experiment_id, archive.run_id)) + f"/artifacts/{ident}"
    assert client.json(base + "/preview")["kind"] == kind
    url = base + "/media"
    status, headers, body = client.request(url)
    assert status == 200 and body == data and headers["Content-Type"] == mime
    assert "media-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    for value, expected in [("bytes=1-9", data[1:10]), ("bytes=-5", data[-5:]),
                            ("bytes=3-", data[3:])]:
        status, headers, body = client.request(url, headers={"Range": value})
        assert status == 206 and body == expected
        assert int(headers["Content-Length"]) == len(expected)
    status, headers, body = client.request(url, method="HEAD", headers={"Range": "bytes=0-3"})
    assert status == 206 and body == b"" and headers["Content-Length"] == "4"
    for value in ("bytes=9999999-", "bytes=8-1", "bytes=0-2,4-8", "bytes=-0", "other=0-1"):
        assert client.request(url, headers={"Range": value})[0] == 416
    assert client.request(url, method="POST", body=b"x")[0] == 405
    assert client.request(base + "/image")[0] == 404


def test_unsupported_media_and_oversize_are_not_served(client, writer):
    archive = writer()
    archive.start()
    for data in (b"<html><script>bad()</script></html>", b"\x1aE\xdf\xa3bad"):
        ident = archive.artifact("deceptive.webm", data)
        url = run_url((archive.experiment_id, archive.run_id)) + f"/artifacts/{ident}/media"
        assert client.request(url)[0] == 404


def test_real_ffmpeg_fixture_is_recognised():
    path = Path(__file__).parent / "fixtures/media/film.webm"
    data = path.read_bytes()
    assert media_type(data[:256 * 1024], len(data)) == "video/webm"


def test_media_browsing_keeps_every_archive_byte_unchanged(client, writer):
    archive = writer()
    archive.start()
    ident = archive.artifact("sound.wav", wav())

    def snapshot():
        return {p.relative_to(archive.directory).as_posix():
                hashlib.sha256(p.read_bytes()).hexdigest()
                for p in archive.directory.rglob("*") if p.is_file()}

    before = snapshot()
    url = run_url((archive.experiment_id, archive.run_id)) + f"/artifacts/{ident}"
    client.json(url + "/preview")
    client.request(url + "/media", headers={"Range": "bytes=0-50"})
    assert snapshot() == before

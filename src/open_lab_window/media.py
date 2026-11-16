"""Bounded passive-media recognition; no decoder, external process or writes."""

import struct

MAX_BYTES = 50 * 1024 * 1024
HEADER_BYTES = 256 * 1024
MAX_SECONDS = 600


def media_type(head: bytes, size: int) -> str | None:
    if not 12 <= size <= MAX_BYTES:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav" if _wav(head, size) else None
    if head[:4] == b"\x1aE\xdf\xa3":
        try:
            return _webm(head, size)
        except (ValueError, IndexError, struct.error, OverflowError):
            return None
    return None


def _wav(data, size):
    if int.from_bytes(data[4:8], "little") + 8 != size:
        return False
    pos, fmt = 12, None
    while pos + 8 <= len(data):
        kind = data[pos:pos + 4]
        length = int.from_bytes(data[pos + 4:pos + 8], "little")
        start, end = pos + 8, pos + 8 + length
        if end > size:
            return False
        if kind == b"fmt ":
            if length < 16 or end > len(data) or fmt is not None:
                return False
            fields = struct.unpack_from("<HHIIHH", data, start)
            code, channels, rate, byte_rate, align, bits = fields
            if (code != 1 or channels not in (1, 2) or not 8000 <= rate <= 48000
                    or bits not in (8, 16, 24, 32) or align != channels * bits // 8
                    or byte_rate != rate * align):
                return False
            fmt = byte_rate, align
        elif kind == b"data":
            return bool(fmt and length % fmt[1] == 0 and 0 < length / fmt[0] <= MAX_SECONDS
                        and end + (length % 2) == size)
        pos = end + (length % 2)
    return False


def _vint(data, pos, *, ident=False):
    first = data[pos]
    if first == 0:
        raise ValueError("invalid EBML integer")
    length = 9 - first.bit_length()
    if length > (4 if ident else 8) or pos + length > len(data):
        raise ValueError("truncated EBML integer")
    value = int.from_bytes(data[pos:pos + length], "big")
    if not ident:
        value &= (1 << (7 * length)) - 1
        if value == (1 << (7 * length)) - 1:
            value = None
    return value, pos + length


def _element(data, pos):
    ident, pos = _vint(data, pos, ident=True)
    length, start = _vint(data, pos)
    return ident, start, None if length is None else start + length


def _children(data, start, end):
    if end is None or end > len(data):
        raise ValueError("header exceeds recognition limit")
    while start < end:
        ident, body, stop = _element(data, start)
        if stop is None or stop > end or stop <= start:
            raise ValueError("bad EBML child")
        yield ident, data[body:stop]
        start = stop


def _fields(data, start, end):
    fields = {}
    for ident, raw in _children(data, start, end):
        if ident in fields:
            raise ValueError("duplicate EBML field")
        fields[ident] = raw
    return fields


def _webm(data, size):
    ident, start, end = _element(data, 0)
    if ident != 0x1A45DFA3 or end is None or end > 4096:
        return None
    header = _fields(data, start, end)
    if header.get(0x4282) != b"webm":
        return None
    ident, start, end = _element(data, end)
    if ident != 0x18538067 or (end is not None and end != size):
        return None
    duration, tracks, cluster = None, None, False
    while start < len(data):
        ident, body, stop = _element(data, start)
        if ident == 0x1F43B675:  # Cluster: payload is left to the browser decoder.
            cluster = body < size and (stop is None or stop <= size)
            break
        if stop is None or stop > len(data) or stop > size:
            return None
        if ident == 0x1549A966:
            info = _fields(data, body, stop)
            scale = int.from_bytes(info.get(0x2AD7B1, b"\x0fB@"), "big")
            raw = info.get(0x4489, b"")
            if len(raw) not in (4, 8):
                return None
            duration = struct.unpack(">f" if len(raw) == 4 else ">d", raw)[0] * scale / 1e9
        elif ident == 0x1654AE6B:
            tracks = list(_children(data, body, stop))
        start = stop
    if (not cluster or duration is None or not 0 < duration <= MAX_SECONDS
            or not tracks or len(tracks) > 4):
        return None
    video = False
    for ident, raw in tracks:
        if ident != 0xAE:
            return None
        fields = _fields(raw, 0, len(raw))
        kind = int.from_bytes(fields.get(0x83, b""), "big")
        codec = fields.get(0x86)
        if kind == 1 and codec in (b"V_VP8", b"V_VP9"):
            raw_video = fields.get(0xE0, b"")
            geometry = _fields(raw_video, 0, len(raw_video))
            width = int.from_bytes(geometry.get(0xB0, b""), "big")
            height = int.from_bytes(geometry.get(0xBA, b""), "big")
            if not 1 <= width <= 1920 or not 1 <= height <= 1080:
                return None
            interval = fields.get(0x23E383)
            if interval is not None and int.from_bytes(interval, "big") < 16_666_666:
                return None
            video = True
        elif kind == 2 and codec in (b"A_OPUS", b"A_VORBIS"):
            raw_audio = fields.get(0xE1, b"")
            audio = _fields(raw_audio, 0, len(raw_audio))
            channels = int.from_bytes(audio.get(0x9F, b"\x01"), "big")
            sample_rate = audio.get(0xB5)
            if sample_rate is not None:
                rate = struct.unpack(">f" if len(sample_rate) == 4 else ">d", sample_rate)[0]
                if not 8000 <= rate <= 48000:
                    return None
            if channels not in (1, 2):
                return None
        else:
            return None
    return "video/webm" if video else None

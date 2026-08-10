"""Format-agnostic WAV reading/slicing shared by the backends.

Deliberately does NOT use Python's `wave` module: it only understands
format tag 1 (integer PCM) and raises on anything else. Confirmed live -
it silently broke (swallowed by a bare except) on a real 32-bit float WAV,
which made channel/rate checks quietly no-op exactly when they mattered.
"""
from __future__ import annotations

from pathlib import Path


def write_wav_interleaved(sources: list[Path], dst: Path) -> bool:
    """Combine per-channel mono WAVs into one interleaved multi-channel WAV.

    Pro Tools records a stereo track as two SEPARATE mono files (`.L`/`.R`),
    not one interleaved file. Confirmed live: a stereo "Synth" track pulled
    from Pro Tools arrived in the shared folder as a single mono `.R` file,
    so the track came back to Reaper stereo-width but mono-content - the
    stereo image was simply gone, silently.

    Sources must be in channel order (left first) and share a format.
    Returns False if they can't be parsed or don't match, so callers can
    fall back rather than write a corrupt file.
    """
    import struct

    if len(sources) < 2:
        return False

    parsed = []
    for src in sources:
        fmt_body, chunks = wav_chunks(src)
        if not fmt_body or len(fmt_body) < 16:
            return False
        tag, channels, rate = struct.unpack("<HHI", fmt_body[:8])
        bits = struct.unpack("<H", fmt_body[14:16])[0]
        data = next((c for c in chunks if c[0] == b"data"), None)
        if data is None or channels != 1 or not bits:
            return False  # only combine genuine mono parts
        parsed.append((src, fmt_body, tag, rate, bits, data[1], data[2]))

    # Every part must agree, or interleaving would silently misalign.
    _s, fmt_body, tag, rate, bits, _at, _sz = parsed[0]
    if any((p[2], p[3], p[4]) != (tag, rate, bits) for p in parsed):
        return False

    sample_bytes = bits // 8
    n_channels = len(parsed)
    frames = min(p[6] // sample_bytes for p in parsed)
    if frames <= 0:
        return False

    channel_data = []
    for _src, _fb, _t, _r, _b, data_at, _size in parsed:
        with open(_src, "rb") as f:
            f.seek(data_at)
            channel_data.append(f.read(frames * sample_bytes))

    # Byte-wise interleave via strided slice assignment - a few C-level
    # slice ops instead of a Python loop over millions of frames, and it
    # works for any sample width including 24-bit, which has no struct code.
    frame_bytes = sample_bytes * n_channels
    out = bytearray(frames * frame_bytes)
    for ch, blob in enumerate(channel_data):
        base = ch * sample_bytes
        for b in range(sample_bytes):
            out[base + b :: frame_bytes] = blob[b::sample_bytes]

    new_fmt = bytearray(fmt_body)
    struct.pack_into("<H", new_fmt, 2, n_channels)              # channels
    struct.pack_into("<I", new_fmt, 8, rate * frame_bytes)      # byte rate
    struct.pack_into("<H", new_fmt, 12, frame_bytes)            # block align

    with open(dst, "wb") as fout:
        fout.write(b"RIFF")
        fout.write(struct.pack("<I", 4 + (8 + len(new_fmt)) + (8 + len(out))))
        fout.write(b"WAVE")
        fout.write(b"fmt ")
        fout.write(struct.pack("<I", len(new_fmt)))
        fout.write(bytes(new_fmt))
        fout.write(b"data")
        fout.write(struct.pack("<I", len(out)))
        fout.write(bytes(out))
    return True


def wav_chunks(path: Path):
    """Yield (chunk_id, offset_of_body, size) for every top-level RIFF
    chunk, plus the raw 'fmt ' body. Format-agnostic on purpose: these
    files can be 32-bit float, which Python's `wave` module refuses.
    """
    import struct

    with open(path, "rb") as f:
        header = f.read(12)
        if header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None, []
        fmt_body = None
        chunks = []
        while True:
            head = f.read(8)
            if len(head) < 8:
                break
            chunk_id, size = struct.unpack("<4sI", head)
            body_at = f.tell()
            if chunk_id == b"fmt ":
                fmt_body = f.read(size)
                f.seek(body_at + size + (size & 1))
            else:
                f.seek(size + (size & 1), 1)
            chunks.append((chunk_id, body_at, size))
        return fmt_body, chunks


def write_wav_slice(src: Path, dst: Path, start_seconds: float, length_seconds: float) -> bool:
    """Write `dst` containing only [start, start+length) of `src`.

    Used so Pro Tools receives audio that is already exactly the right
    region, which removes the need to trim after spotting. That matters:
    `trim_to_selection` acts on the edit selection rather than one clip
    and was confirmed live to delete a neighbouring clip on the same
    track. Slicing up front also finally honours Clip.source_offset,
    which the Pro Tools path otherwise ignored completely.

    Returns False if the file can't be parsed, so callers fall back to
    importing it whole rather than failing the push.
    """
    import struct

    fmt_body, chunks = wav_chunks(src)
    if not fmt_body or len(fmt_body) < 16:
        return False
    _tag, channels, sample_rate = struct.unpack("<HHI", fmt_body[:8])
    bits = struct.unpack("<H", fmt_body[14:16])[0]
    if not channels or not sample_rate or not bits:
        return False

    data = next((c for c in chunks if c[0] == b"data"), None)
    if data is None:
        return False
    _id, data_at, data_size = data

    frame_bytes = channels * (bits // 8)
    if frame_bytes <= 0:
        return False

    start_byte = max(0, int(round(start_seconds * sample_rate)) * frame_bytes)
    want_bytes = max(0, int(round(length_seconds * sample_rate)) * frame_bytes)
    start_byte = min(start_byte, data_size)
    want_bytes = min(want_bytes, data_size - start_byte)
    if want_bytes <= 0:
        return False

    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(b"RIFF")
        fout.write(struct.pack("<I", 4 + (8 + len(fmt_body)) + (8 + want_bytes)))
        fout.write(b"WAVE")
        fout.write(b"fmt ")
        fout.write(struct.pack("<I", len(fmt_body)))
        fout.write(fmt_body)
        fout.write(b"data")
        fout.write(struct.pack("<I", want_bytes))
        fin.seek(data_at + start_byte)
        remaining = want_bytes
        while remaining > 0:
            chunk = fin.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            fout.write(chunk)
            remaining -= len(chunk)
    return True


def read_wav_fmt(path: Path) -> tuple[int, int] | None:
    """(channels, sample_rate) read directly from the WAV "fmt " chunk, or
    None if it can't be parsed. Deliberately not using Python's `wave`
    module: it only understands format tag 1 (integer PCM) and raises on
    anything else - confirmed live, it silently broke (caught by a bare
    except) on a real 32-bit float WAV, which made channel/rate checks
    quietly no-op exactly when they mattered most for that file.
    """
    import struct

    try:
        with open(path, "rb") as f:
            header = f.read(12)
            if header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            while True:
                chunk_header = f.read(8)
                if len(chunk_header) < 8:
                    return None
                chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
                if chunk_id == b"fmt ":
                    body = f.read(chunk_size)
                    _fmt_tag, channels, sample_rate = struct.unpack("<HHI", body[:8])
                    return channels, sample_rate
                f.seek(chunk_size + (chunk_size & 1), 1)  # chunks are word-aligned
    except Exception:
        return None


def channel_count(path: Path) -> int | None:
    """Best-effort channel count for a .wav file. None if undeterminable,
    so callers should treat that as "unknown, don't block".
    """
    fmt = read_wav_fmt(path)
    return fmt[0] if fmt else None


def sample_rate(path: Path) -> int | None:
    """Best-effort sample rate for a .wav file. None if undeterminable."""
    fmt = read_wav_fmt(path)
    return fmt[1] if fmt else None


def duration_seconds(path: Path) -> float | None:
    """Best-effort duration of a .wav file, from the data chunk size and
    the fmt header's byte rate. None if undeterminable.
    """
    import struct

    fmt = read_wav_fmt(path)
    if not fmt:
        return None
    channels, sample_rate = fmt
    try:
        with open(path, "rb") as f:
            f.seek(12)
            bits = None
            while True:
                chunk_header = f.read(8)
                if len(chunk_header) < 8:
                    return None
                chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
                if chunk_id == b"fmt ":
                    body = f.read(chunk_size)
                    bits = struct.unpack("<H", body[14:16])[0]
                    continue
                if chunk_id == b"data":
                    if not bits or not channels or not sample_rate:
                        return None
                    frames = chunk_size / (channels * (bits / 8))
                    return frames / float(sample_rate)
                f.seek(chunk_size + (chunk_size & 1), 1)
    except Exception:
        return None

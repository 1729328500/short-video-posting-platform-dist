# -*- coding: utf-8 -*-
"""生成极简 MP4 测试文件（含正确的 moov/mvhd/tkhd 元信息：时长与分辨率）"""
import struct
import sys


def _box(typ, payload):
    return struct.pack(">I", 8 + len(payload)) + typ.encode("latin-1") + payload


def build_mp4(path, seconds=12.0, width=1080, height=1920, timescale=1000):
    dur = int(seconds * timescale)
    ftyp = _box("ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")

    mvhd = _box(
        "mvhd",
        bytes([0, 0, 0, 0])
        + struct.pack(">IIII", 0, 0, timescale, dur)
        + struct.pack(">I", 0x00010000)
        + struct.pack(">HH", 0x0100, 0)
        + struct.pack(">II", 0, 0)
        + struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
        + bytes(24)
        + struct.pack(">I", 2),
    )
    tkhd = _box(
        "tkhd",
        bytes([0, 0, 0, 0])
        + struct.pack(">IIII", 0, 0, 1, 0)
        + struct.pack(">I", dur)
        + struct.pack(">II", 0, 0)
        + struct.pack(">HHHH", 0, 0, 0, 0)
        + struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
        + struct.pack(">II", width << 16, height << 16),
    )
    mdhd = _box(
        "mdhd",
        bytes([0, 0, 0, 0])
        + struct.pack(">IIII", 0, 0, timescale, dur)
        + struct.pack(">HH", 0x55C4, 0),
    )
    hdlr = _box("hdlr", bytes([0, 0, 0, 0]) + b"\0\0\0\0" + b"vide" + bytes(12) + b"VideoHandler\0")
    stsd = _box("stsd", bytes(8))
    minf = _box("minf", _box("stbl", stsd))
    mdia = _box("mdia", mdhd + hdlr + minf)
    moov = _box("moov", mvhd + _box("trak", tkhd + mdia))
    mdat = _box("mdat", b"\0" * 2048)

    with open(path, "wb") as f:
        f.write(ftyp + moov + mdat)
    return str(path)


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else "test.mp4"
    sec = float(sys.argv[2]) if len(sys.argv) > 2 else 12.0
    print(build_mp4(p, seconds=sec))

"""Rysuje ikone aplikacji (instalator/ikona.ico) bez zewnetrznych bibliotek: zaokraglony kwadrat z gradientem i bialy wykres w gore.
Uruchomienie: python instalator/make_icon.py  (plik .ico jest w repozytorium - skrypt jest tylko po to, zeby dalo sie go odtworzyc)."""
import math, os, struct, zlib

C1, C2 = (37, 99, 235), (139, 92, 246)            # niebieski -> fioletowy (jak na stronie)
LINE = [(0.20, 0.70), (0.42, 0.48), (0.58, 0.60), (0.80, 0.30)]


def seg_dist(px, py, a, b):
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def pixel(x, y):
    """(r, g, b, a) dla punktu w [0,1]^2."""
    r = 0.22                                           # promien zaokraglenia
    cx, cy = min(max(x, r), 1 - r), min(max(y, r), 1 - r)
    if math.hypot(x - cx, y - cy) > r:
        return (0, 0, 0, 0)
    t = (x + y) / 2
    col = tuple(int(C1[i] + (C2[i] - C1[i]) * t) for i in range(3))
    d = min(seg_dist(x, y, LINE[i], LINE[i + 1]) for i in range(len(LINE) - 1))
    tip = LINE[-1]
    if d < 0.055 or math.hypot(x - tip[0], y - tip[1]) < 0.075:
        return (255, 255, 255, 255)
    return (*col, 255)


def render(size, ss=4):
    rows = []
    for j in range(size):
        row = bytearray()
        for i in range(size):
            acc = [0, 0, 0, 0]
            for sj in range(ss):
                for si in range(ss):
                    p = pixel((i + (si + 0.5) / ss) / size, (j + (sj + 0.5) / ss) / size)
                    a = p[3]
                    acc[0] += p[0] * a; acc[1] += p[1] * a; acc[2] += p[2] * a; acc[3] += a
            n = ss * ss
            a = acc[3] / n
            row += bytes([int(acc[0] / acc[3]) if acc[3] else 0, int(acc[1] / acc[3]) if acc[3] else 0, int(acc[2] / acc[3]) if acc[3] else 0, int(a)])
        rows.append(bytes(row))
    return rows


def png(size):
    raw = b"".join(b"\x00" + r for r in render(size))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def ico(sizes=(16, 24, 32, 48, 64, 128, 256)):
    images = [png(s) for s in sizes]
    head = struct.pack("<HHH", 0, 1, len(sizes))
    off = 6 + 16 * len(sizes); dirs = b""
    for s, data in zip(sizes, images):
        dirs += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), off); off += len(data)
    return head + dirs + b"".join(images)


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ikona.ico")
    with open(out, "wb") as f:
        f.write(ico())
    print(out)

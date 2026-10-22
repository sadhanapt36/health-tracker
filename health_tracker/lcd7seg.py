"""Read a home blood pressure monitor's seven-segment LCD, entirely on this computer.

Built and tested on an Omron monitor with three rows: SYS, DIA, PULSE. Classic
computer vision (OpenCV): no machine learning, no network, no cost.

  1. find the LCD panel (a bright rectangle inside a dark bezel);
  2. mark "ink" (darker than its surroundings) and keep only segment-bar shapes;
  3. measure the bar size, so it works at any camera distance;
  4. undo the display's italic slant;
  5. split into three rows and digits, and read which of the 7 segments are on.

Deliberately strict: if anything looks wrong it returns None instead of a guess.
Whatever it returns is still only a DRAFT for a person to confirm.
"""
from typing import Optional

import cv2
import numpy as np

# Segments: a top, b upper-right, c lower-right, d bottom, e lower-left, f upper-left, g middle.
_PATTERNS = {
    "abcdef": 0, "bc": 1, "abdeg": 2, "abcdg": 3, "bcfg": 4, "acdfg": 5,
    "acdefg": 6, "abc": 7, "abcdefg": 8, "abcdfg": 9,
    "abcf": 7,                      # Omron draws a 7 with a small upper-left bar
}
_MAX_SIDE = 1600


def _touches(x, y, w, h, W, H):
    return x <= 1 or y <= 1 or x + w >= W - 1 or y + h >= H - 1


def _ink(gray, frac=0.18):
    H, W = gray.shape
    blur = cv2.GaussianBlur(gray, (0, 0), 1.5)
    bg = cv2.GaussianBlur(blur, (0, 0), max(10, 0.04 * W))
    spread = float(np.percentile(blur, 95) - np.percentile(blur, 5))
    delta = max(12.0, frac * spread)
    return ((bg.astype(np.int16) - blur.astype(np.int16)) > delta).astype(np.uint8)


def _contrast(gray):
    """How much darker each pixel is than its surroundings (0 = not at all)."""
    H, W = gray.shape
    blur = cv2.GaussianBlur(gray, (0, 0), 1.5)
    bg = cv2.GaussianBlur(blur, (0, 0), max(10, 0.04 * W))
    return np.clip(bg.astype(np.float32) - blur.astype(np.float32), 0, None)


def _runs(flags, min_gap):
    runs, start, last = [], None, None
    for i, f in enumerate(flags):
        if f:
            if start is None:
                start = i
            elif i - last > min_gap:
                runs.append((start, last))
                start = i
            last = i
    if start is not None:
        runs.append((start, last))
    return runs


def _shear(mask, s):
    H, W = mask.shape
    shifts = np.round(s * np.arange(H)).astype(int)
    shifts -= shifts.min()
    out = np.zeros((H, W + int(shifts.max()) + 1), mask.dtype)
    for y in range(H):
        out[y, shifts[y]:shifts[y] + W] = mask[y]
    return out


def segment_mask(gray, frac=0.18):
    """Ink mask containing only segment-bar-sized pieces, plus (L, t) bar length/thickness."""
    H, W = gray.shape
    ink = _ink(gray, frac)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink)
    bars = []
    for x, y, w, h, a in stats[1:]:
        lo, hi = min(w, h), max(w, h)
        if (not _touches(x, y, w, h, W, H) and 8 <= lo <= 80
                and 2.4 <= hi / lo <= 6.0 and a >= 0.5 * lo * hi):
            bars.append((hi, lo))
    if len(bars) < 8:
        return None, None, None
    L = float(np.median([b[0] for b in bars]))
    t = float(np.median([b[1] for b in bars]))
    keep = np.zeros(n, bool)
    for i, (x, y, w, h, a) in enumerate(stats[1:], start=1):
        if (not _touches(x, y, w, h, W, H) and 0.6 * L <= max(w, h) <= 2.8 * L
                and min(w, h) >= 0.5 * t and max(w, h) <= 3.2 * L):
            keep[i] = True
    return keep[lab].astype(np.uint8), L, t


def _read_cell(sh, cx0, cx1, y0, L, t, DH, Wd):
    H, W = sh.shape
    x1 = cx1 + 1
    x0 = x1 - Wd
    yc_up = y0 + 0.25 * t + L / 2
    yc_lo = y0 + DH - 0.25 * t - L / 2

    def on(cx, cy, hw, hh):
        xa, xb = int(round(cx - hw)), int(round(cx + hw))
        ya, yb = int(round(cy - hh)), int(round(cy + hh))
        patch = sh[max(ya, 0):min(yb, H), max(xa, 0):min(xb, W)]
        return patch.size > 0 and float(patch.mean()) >= 0.5

    right, left, mid = x1 - t / 2, x0 + t / 2, (x0 + x1) / 2
    seg = {
        "a": on(mid, y0 + t / 2, 0.3 * L, 0.3 * t),
        "g": on(mid, y0 + DH / 2, 0.3 * L, 0.3 * t),
        "d": on(mid, y0 + DH - t / 2, 0.3 * L, 0.3 * t),
        "b": on(right, yc_up, 0.3 * t, 0.3 * L),
        "c": on(right, yc_lo, 0.3 * t, 0.3 * L),
        "f": on(left, yc_up, 0.3 * t, 0.3 * L),
        "e": on(left, yc_lo, 0.3 * t, 0.3 * L),
    }
    if (cx1 - cx0 + 1) < 0.55 * Wd:            # only right-hand bars: a "1"
        seg = {k: (k in "bc" and seg[k]) for k in seg}
    return _PATTERNS.get("".join(k for k in "abcdefg" if seg[k]))


def decode_display(gray, debug=None):
    """Gray image of (roughly) the LCD -> [sys, dia, pulse] ints, or None.
    Blurry or distant photos need a stricter ink threshold to keep segments apart,
    so several are tried; the first that yields three clean rows wins."""
    for frac in (0.18, 0.25, 0.32, 0.40):
        d = {} if debug is not None else None
        vals = _decode_at(gray, frac, d)
        if debug is not None: debug.update(d, frac=frac)
        if vals:
            return vals
    return None


def _decode_at(gray, frac, debug=None):
    mask, L, t = segment_mask(gray, frac)
    if mask is None or mask.sum() == 0:
        if debug is not None: debug["why"] = "no segment bars"
        return None
    best_s, best = 0.0, -1.0
    for s in np.linspace(-0.2, 0.2, 41):
        score = float((_shear(mask, s).sum(axis=0).astype(np.float64) ** 2).sum())
        if score > best:
            best_s, best = float(s), score
    sh = _shear(mask, best_s)
    C = _contrast(gray)
    typical = float(np.median(C[mask > 0]))
    shc = _shear(np.clip(C / max(typical, 1e-3), 0, 1.5).astype(np.float32), best_s)
    DH, Wd = 2 * L + t, L + t
    rows = _runs(sh.sum(axis=1) > 0, max(2, int(0.9 * t)))
    rows = [r for r in rows if r[1] - r[0] >= 0.5 * DH]
    if debug is not None:
        debug.update(L=L, t=t, shear=best_s, rows=rows)
    if len(rows) != 3:
        if debug is not None: debug["why"] = f"{len(rows)} rows"
        return None
    values = []
    for ry0, ry1 in rows:
        if not 0.5 * DH <= ry1 - ry0 + 1 <= 1.3 * DH:
            if debug is not None: debug["why"] = "row too tall"
            return None
        band = sh[ry0:ry1 + 1]
        # Rows can differ in size (the pulse digits are smaller), so scale per row.
        k = (ry1 - ry0 + 1) / DH
        Lr, tr, DHr, Wdr = L * k, t * k, DH * k, Wd * k
        digits = [_read_cell(shc, a, b, ry0, Lr, tr, DHr, Wdr)
                  for a, b in _runs(band.sum(axis=0) > 0, max(2, int(0.7 * tr)))]
        if debug is not None: debug.setdefault("digits", []).append(digits)
        if not digits or len(digits) > 3 or any(d is None for d in digits):
            if debug is not None: debug["why"] = "unreadable digit"
            return None
        values.append(int("".join(map(str, digits))))
    return values


def lcd_candidates(gray):
    H, W = gray.shape
    b = cv2.GaussianBlur(gray, (0, 0), max(2, 0.003 * max(H, W)))
    thr, _ = cv2.threshold(b, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    mask = (b > thr * 0.9).astype(np.uint8)
    k = max(5, int(0.017 * max(H, W)))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    out = []
    for x, y, w, h, a in stats[1:]:
        if a < 0.02 * H * W or w > 0.95 * W or not 0.6 <= w / h <= 1.6 or a < 0.5 * w * h:
            continue
        out.append((int(a), (int(x), int(y), int(w), int(h))))
    return [box for _, box in sorted(out, reverse=True)[:3]]


def read_bp_display(data: bytes) -> Optional[dict]:
    """Photo bytes -> {"systolic","diastolic","pulse"} or None if not a clearly readable monitor."""
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    big = max(img.shape)
    if big > _MAX_SIDE:
        img = cv2.resize(img, None, fx=_MAX_SIDE / big, fy=_MAX_SIDE / big, interpolation=cv2.INTER_AREA)
    for x, y, w, h in lcd_candidates(img):
        px, py = int(0.03 * w), int(0.03 * h)
        crop = img[y + py:y + h - py, x + px:x + w - px]
        try:
            vals = decode_display(crop)
        except Exception:
            vals = None
        if vals:
            return {"systolic": vals[0], "diastolic": vals[1], "pulse": vals[2]}
    return None

"""Draw the OpenCDi logo (a compact disc with an "i" in the middle).

    python tools/make_icon.py

Writes opencdi/data/opencdi.png (the window icon) and the PNG sizes the Debian
package installs under packaging/debian/icons/.
"""

import os
import sys
from pathlib import Path

import numpy as np

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
import pygame  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SS = 4  # supersampling factor for antialiasing
SIZES = (16, 32, 48, 64, 128, 256)


def hsv_rainbow(h):
    """h in 0..1 -> rgb array 0..1 (full saturation and value)."""
    k = (np.stack([h * 6 + 5, h * 6 + 3, h * 6 + 1], -1)) % 6
    return 1 - np.clip(np.minimum(k, 4 - k), 0, 1)


def draw(size):
    n = size * SS
    c = (np.arange(n) + 0.5) / n * 2 - 1
    x, y = np.meshgrid(c, c)
    r = np.hypot(x, y)
    theta = np.arctan2(y, x)

    rgb = np.zeros((n, n, 3))
    alpha = np.zeros((n, n))

    def paint(mask, color, a=1.0):
        color = np.asarray(color, dtype=float)
        if color.ndim == 1:
            color = color / 255.0
        m = mask * a
        rgb[:] = rgb * (1 - m[..., None]) + color * m[..., None]
        alpha[:] = np.maximum(alpha, mask)

    # dark rim, then the silver disc with two opposite rainbow reflections
    paint(r < 0.98, (75, 83, 99))
    silver = np.empty((n, n, 3))
    shade = 0.80 + 0.12 * np.cos(theta - 2.4)
    silver[:] = shade[..., None] * np.array([0.93, 0.95, 1.0])
    band = np.maximum(0, np.cos(2 * (theta + 0.75))) ** 4
    rainbow = hsv_rainbow((r * 1.6 + theta / np.pi * 0.15) % 1.0)
    silver = silver * (1 - 0.45 * band[..., None]) + rainbow * 0.45 * band[..., None]
    glint = np.maximum(0, np.cos(2 * (theta - 0.85))) ** 30
    silver = silver + (1 - silver) * 0.7 * glint[..., None]
    paint(r < 0.94, silver)
    paint((r > 0.875) & (r < 0.89), (255, 255, 255), 0.6)

    # clamping ring and the blue label with a white "i"
    paint(r < 0.54, (141, 151, 168))
    paint(r < 0.515, (223, 228, 236))
    paint(r < 0.44, (28, 63, 148))
    dot = np.hypot(x, y + 0.2) < 0.09
    # stem: rounded rectangle, half width 0.08, from y=-0.07 to 0.27, corner radius 0.03
    rad = 0.03
    qx = np.maximum(np.abs(x) - (0.08 - rad), 0)
    qy = np.maximum(np.abs(y - 0.10) - (0.17 - rad), 0)
    stem = np.hypot(qx, qy) < rad
    paint(dot | stem, (255, 255, 255))

    # downsample (premultiplied) for antialiasing
    pre = np.concatenate([rgb * alpha[..., None], alpha[..., None]], -1)
    pre = pre.reshape(size, SS, size, SS, 4).mean((1, 3))
    a = pre[..., 3:]
    out = np.where(a > 0, pre[..., :3] / np.maximum(a, 1e-9), 0)
    return np.concatenate([out, a], -1)


def save(img, path):
    data = (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8)
    h, w = data.shape[:2]
    surf = pygame.image.frombuffer(data.tobytes(), (w, h), 'RGBA')
    path.parent.mkdir(parents=True, exist_ok=True)
    pygame.image.save(surf, str(path))
    print(f'wrote {path.relative_to(ROOT)}')


def main():
    save(draw(256), ROOT / 'opencdi' / 'data' / 'opencdi.png')
    for s in SIZES:
        save(draw(s), ROOT / 'packaging' / 'debian' / 'icons' / f'{s}x{s}' / 'opencdi.png')
    return 0


if __name__ == '__main__':
    sys.exit(main())

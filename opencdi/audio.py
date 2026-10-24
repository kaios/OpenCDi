"""Audio stream helpers shared by the machine and the frontend."""

import math

import numpy as np

OUT_RATE = 44100


class LinearResampler:
    """Streaming linear-interpolation resampler for (n, 2) sample blocks.

    Unlike resampling each block on its own, the read position and the last
    input sample carry over from one block to the next, so block boundaries
    stay seamless (no clicks, no stretched or dropped samples).
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.pos = 0.0      # read position relative to self.last (index 0)
        self.last = None    # last input sample of the previous block, shape (1, 2)

    def process(self, x, step):
        """Resample x (n, 2) float32; step = input samples per output sample."""
        if len(x) == 0:
            return np.zeros((0, 2), dtype=np.float32)
        buf = x if self.last is None else np.concatenate([self.last, x])
        avail = len(buf) - 1            # outputs need buf[i] and buf[i + 1]
        if avail <= 0 or self.pos >= avail:
            k = 0
        else:
            k = int(math.ceil((avail - self.pos) / step))
            if self.pos + (k - 1) * step >= avail:
                k -= 1
        if k > 0:
            ps = self.pos + step * np.arange(k)
            i = ps.astype(np.int64)
            f = (ps - i).astype(np.float32)[:, None]
            out = buf[i] * (1 - f) + buf[i + 1] * f
            self.pos = self.pos + k * step - avail
        else:
            out = np.zeros((0, 2), dtype=np.float32)
            self.pos -= avail
        self.last = buf[-1:].copy()
        return out.astype(np.float32, copy=False)

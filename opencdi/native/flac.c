/* FLAC frame decoder for the audio hunks of CHD files ("cdfl"): bare FLAC
 * frames, 16-bit stereo.  Same algorithm as opencdi/flac.py, which is the
 * reference and the fallback.
 *
 * flac_decode_cd() decodes frames until nsamples per channel are out, writes
 * them interleaved and big-endian (the way CHD stores CD audio) and returns
 * the number of input bytes used, or a negative error code.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

typedef struct {
    const uint8_t *data;
    size_t bits, pos;
    int err;
} bitin;

static inline uint32_t bits_read(bitin *b, int n)
{
    if (n == 0)
        return 0;
    if (b->pos + (size_t)n > b->bits) {
        b->err = 1;
        b->pos = b->bits;
        return 0;
    }
    uint32_t v = 0;
    size_t p = b->pos;
    int left = n;
    while (left > 0) {
        int off = (int)(p & 7);
        int take = 8 - off < left ? 8 - off : left;
        uint32_t byte = b->data[p >> 3];
        v = (v << take) | ((byte >> (8 - off - take)) & ((1u << take) - 1));
        p += take;
        left -= take;
    }
    b->pos = p;
    return v;
}

static inline int32_t bits_signed(bitin *b, int n)
{
    if (n == 0)
        return 0;
    uint32_t v = bits_read(b, n);
    if (n < 32 && (v >> (n - 1)) & 1)
        return (int32_t)(v | ~((1u << n) - 1));
    return (int32_t)v;
}

static inline uint32_t bits_unary(bitin *b)
{
    uint32_t n = 0;
    while (b->pos < b->bits) {
        size_t p = b->pos;
        uint8_t byte = (uint8_t)(b->data[p >> 3] << (p & 7));
        if (byte) {
            int z = 0;
            while (!(byte & 0x80)) {
                byte <<= 1;
                z++;
            }
            b->pos = p + z + 1;
            return n + z;
        }
        n += 8 - (p & 7);
        b->pos = (p | 7) + 1;
    }
    b->err = 1;
    return 0;
}

static inline void bits_align(bitin *b) { b->pos = (b->pos + 7) & ~(size_t)7; }

#define MAX_BLOCK 65536
static int32_t chan[2][MAX_BLOCK];

static int residual(bitin *b, int blocksize, int order, int32_t *out)
{
    int method = (int)bits_read(b, 2);
    if (method > 1)
        return -1;
    int pbits = method == 0 ? 4 : 5, escape = method == 0 ? 15 : 31;
    int porder = (int)bits_read(b, 4);
    int parts = 1 << porder, n = blocksize >> porder, i = order;
    for (int p = 0; p < parts; p++) {
        int count = p == 0 ? n - order : n;
        if (count < 0 || i + count > blocksize)
            return -1;
        int k = (int)bits_read(b, pbits);
        if (k == escape) {
            int raw = (int)bits_read(b, 5);
            for (int j = 0; j < count; j++)
                out[i++] = bits_signed(b, raw);
        } else {
            for (int j = 0; j < count; j++) {
                uint32_t v = (bits_unary(b) << k) | bits_read(b, k);
                out[i++] = (int32_t)(v >> 1) ^ -(int32_t)(v & 1);
            }
        }
        if (b->err)
            return -1;
    }
    return 0;
}

static const int32_t FIXED[5][4] = {{0}, {1}, {2, -1}, {3, -3, 1}, {4, -6, 4, -1}};

static int subframe(bitin *b, int blocksize, int bps, int32_t *s)
{
    if (bits_read(b, 1))
        return -1;
    int kind = (int)bits_read(b, 6);
    int wasted = 0;
    if (bits_read(b, 1)) {
        wasted = (int)bits_unary(b) + 1;
        bps -= wasted;
    }
    if (bps <= 0 || bps > 32)
        return -1;
    if (kind == 0) {
        int32_t v = bits_signed(b, bps);
        for (int i = 0; i < blocksize; i++)
            s[i] = v;
    } else if (kind == 1) {
        for (int i = 0; i < blocksize; i++)
            s[i] = bits_signed(b, bps);
    } else if ((kind >= 8 && kind <= 12) || kind >= 32) {
        int order, shift = 0;
        int32_t coefs[32];
        if (kind >= 32) {
            order = kind - 31;
            if (order > blocksize)
                return -1;
            for (int i = 0; i < order; i++)
                s[i] = bits_signed(b, bps);
            int prec = (int)bits_read(b, 4) + 1;
            if (prec == 16)
                return -1;
            shift = bits_signed(b, 5);
            if (shift < 0)
                return -1;
            for (int i = 0; i < order; i++)
                coefs[i] = bits_signed(b, prec);
        } else {
            order = kind - 8;
            if (order > blocksize)
                return -1;
            for (int i = 0; i < order; i++)
                s[i] = bits_signed(b, bps);
            for (int i = 0; i < order; i++)
                coefs[i] = FIXED[order][i];
        }
        if (residual(b, blocksize, order, s))
            return -1;
        for (int i = order; i < blocksize; i++) {
            int64_t acc = 0;
            for (int c = 0; c < order; c++)
                acc += (int64_t)coefs[c] * s[i - 1 - c];
            s[i] += (int32_t)(acc >> shift);
        }
    } else {
        return -1;
    }
    if (wasted)
        for (int i = 0; i < blocksize; i++)
            s[i] = (int32_t)((uint32_t)s[i] << wasted);
    return b->err ? -1 : 0;
}

static const int BLOCK_SIZES[16] = {0, 192, 576, 1152, 2304, 4608, 0, 0, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768};
static const int SAMPLE_BITS[8] = {0, 8, 12, 0, 16, 20, 24, 32};

long flac_decode_cd(const uint8_t *src, long len, uint8_t *out, int nsamples)
{
    bitin b = {src, (size_t)len * 8, 0, 0};
    int done = 0;
    while (done < nsamples) {
        bits_align(&b);
        if (bits_read(&b, 15) != 0x7ffc)
            return -2;                          /* no frame sync */
        bits_read(&b, 1);
        int bs_code = (int)bits_read(&b, 4), sr_code = (int)bits_read(&b, 4);
        int ch_code = (int)bits_read(&b, 4), ss_code = (int)bits_read(&b, 3);
        bits_read(&b, 1);
        uint32_t v = bits_read(&b, 8);          /* UTF-8 coded frame number */
        if (v & 0x80) {
            int n = 0;
            for (uint32_t m = 0x40; v & m; m >>= 1)
                n++;
            for (int i = 0; i < n; i++)
                bits_read(&b, 8);
        }
        int blocksize;
        if (bs_code == 6)
            blocksize = (int)bits_read(&b, 8) + 1;
        else if (bs_code == 7)
            blocksize = (int)bits_read(&b, 16) + 1;
        else
            blocksize = BLOCK_SIZES[bs_code];
        if (blocksize <= 0 || blocksize > MAX_BLOCK)
            return -3;
        if (sr_code == 12)
            bits_read(&b, 8);
        else if (sr_code == 13 || sr_code == 14)
            bits_read(&b, 16);
        bits_read(&b, 8);                       /* header CRC-8 */
        int bps = ss_code ? SAMPLE_BITS[ss_code] : 16;
        if (!bps)
            return -3;
        if (ch_code == 1) {
            if (subframe(&b, blocksize, bps, chan[0]) || subframe(&b, blocksize, bps, chan[1]))
                return -4;
        } else if (ch_code >= 8 && ch_code <= 10) {
            if (subframe(&b, blocksize, bps + (ch_code == 9), chan[0]) ||
                subframe(&b, blocksize, bps + (ch_code != 9), chan[1]))
                return -4;
            int32_t *a = chan[0], *c = chan[1];
            for (int i = 0; i < blocksize; i++) {
                if (ch_code == 8) {            /* left / side */
                    c[i] = a[i] - c[i];
                } else if (ch_code == 9) {     /* side / right */
                    a[i] = a[i] + c[i];
                } else {                       /* mid / side */
                    int32_t m = (int32_t)((uint32_t)a[i] << 1) | (c[i] & 1), sd = c[i];
                    a[i] = (m + sd) >> 1;
                    c[i] = (m - sd) >> 1;
                }
            }
        } else {
            return -5;                          /* CD audio is stereo */
        }
        bits_align(&b);
        bits_read(&b, 16);                      /* frame CRC-16 */
        if (b.err)
            return -6;
        for (int i = 0; i < blocksize && done < nsamples; i++, done++) {
            uint8_t *o = out + (size_t)done * 4;
            uint16_t l = (uint16_t)chan[0][i], r = (uint16_t)chan[1][i];
            o[0] = (uint8_t)(l >> 8);
            o[1] = (uint8_t)l;
            o[2] = (uint8_t)(r >> 8);
            o[3] = (uint8_t)r;
        }
    }
    return (long)(b.pos >> 3);
}

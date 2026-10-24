/* Native versions of the two most expensive picture steps of opencdi/mcd212.py:
 * DYUV decoding (MCD212.render_dyuv + yuv_to_rgb) and plane mixing
 * (MCD212.mix after the mosaic step).  The lookup tables come from mcd212.py,
 * so the results are bit-identical to the numpy code, which stays as the
 * fallback.
 */
#include <stddef.h>
#include <stdint.h>

typedef int64_t i64;

static inline int32_t clip2ff(int32_t x) { return x < 0 ? 0 : x > 0x2ff ? 0x2ff : x; }

static inline uint32_t yuv(int y, int u, int v, const int32_t *limit_fe, const int32_t *u_to_b,
                           const int32_t *u_to_g, const int32_t *v_to_g, const int32_t *v_to_r)
{
    int32_t y0 = y + 0x100;
    uint32_t r = (uint32_t)limit_fe[clip2ff(y0 + v_to_r[v])];
    uint32_t g = (uint32_t)limit_fe[clip2ff(y0 + ((u_to_g[u] + v_to_g[v]) >> 8))];
    uint32_t b = (uint32_t)limit_fe[clip2ff(y0 + u_to_b[u])];
    return r << 16 | g << 8 | b;
}

/* n lines of DYUV, line l starting at plane byte vsrs[l]; out is n x width */
void vid_dyuv(const uint8_t *data, const i64 *vsrs, int n, int width, i64 start, const int32_t *deltas,
              const int32_t *limit_fe, const int32_t *u_to_b, const int32_t *u_to_g, const int32_t *v_to_g,
              const int32_t *v_to_r, uint32_t color_off, uint32_t *out)
{
    int pairs = width / 4;
    int ys[2 * 192], us[192], vs[192];
    if (pairs > 192)
        pairs = 192;
    for (int l = 0; l < n; l++) {
        uint32_t *o = out + (size_t)l * width;
        i64 vsr = vsrs[l];
        int y = (int)(start >> 16) & 0xff, u = (int)(start >> 8) & 0xff, v = (int)start & 0xff;
        for (int k = 0; k < pairs; k++) {
            int b0 = data[(vsr + 2 * k) & 0x7ffff];
            int b1 = data[(vsr + 2 * k + 1) & 0x7ffff];
            y = (y + deltas[b0 & 15]) & 0xff;
            ys[2 * k] = y;
            y = (y + deltas[b1 & 15]) & 0xff;
            ys[2 * k + 1] = y;
            u = (u + deltas[b0 >> 4]) & 0xff;
            us[k] = u;
            v = (v + deltas[b1 >> 4]) & 0xff;
            vs[k] = v;
        }
        for (int k = 0; k < pairs; k++) {
            int un = k + 1 < pairs ? us[k + 1] : us[k];
            int vn = k + 1 < pairs ? vs[k + 1] : vs[k];
            uint32_t c0 = yuv(ys[2 * k], us[k], vs[k], limit_fe, u_to_b, u_to_g, v_to_g, v_to_r);
            uint32_t c1 = yuv(ys[2 * k + 1], ((us[k] + un) >> 1) & 0xff, ((vs[k] + vn) >> 1) & 0xff,
                              limit_fe, u_to_b, u_to_g, v_to_g, v_to_r);
            o[4 * k] = o[4 * k + 1] = c0;
            o[4 * k + 2] = o[4 * k + 3] = c1;
        }
        for (int x = 4 * pairs; x < width; x++)
            o[x] = color_off;
    }
}

/* mix two planes (n x width colours + transparency) into out (n x 768) */
void vid_mix(const uint32_t *ca, const uint8_t *ta, const uint32_t *cb, const uint8_t *tb, int n, int width,
             const int32_t *wa, const int32_t *wb, int disable_mx, int order_ba, uint32_t backdrop,
             const int32_t *limit, const uint32_t *expand, int border, uint32_t color_off, uint32_t *out)
{
    for (int l = 0; l < n; l++) {
        const uint32_t *pa_row = ca + (size_t)l * width, *pb_row = cb + (size_t)l * width;
        const uint8_t *ta_row = ta + (size_t)l * width, *tb_row = tb + (size_t)l * width;
        uint32_t *o = out + (size_t)l * 768;
        for (int x = 0; x < border; x++)
            o[x] = 0;
        for (int x = border + width; x < 768; x++)
            o[x] = 0;
        o += border;
        for (int x = 0; x < width; x++) {
            int at = ta_row[x], bt = tb_row[x];
            if (at && bt) {
                o[x] = backdrop;
                continue;
            }
            uint32_t a = at ? color_off : pa_row[x];
            uint32_t b = bt ? color_off : pb_row[x];
            if (disable_mx) {
                if (!order_ba) {
                    if (!at)
                        b = color_off;
                } else if (!bt) {
                    a = color_off;
                }
            }
            uint32_t res = 0;
            for (int s = 16; s >= 0; s -= 8) {
                int32_t pa = (a >> s) & 0xff, pb = (b >> s) & 0xff;
                int32_t m = limit[(((pa - 16) * wa[x]) >> 6) + (((pb - 16) * wb[x]) >> 6) + 0x110];
                res |= expand[m] << s;
            }
            o[x] = res;
        }
    }
}

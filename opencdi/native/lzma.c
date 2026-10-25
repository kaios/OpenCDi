/* LZMA decoder for the "cdlz" hunks of CHD files: raw LZMA1 data (no header,
 * no end marker needed) with lc=3 lp=0 pb=2, decoded into a buffer whose
 * size is known.  Written from Igor Pavlov's public-domain LZMA specification
 * decoder (LzmaSpec.cpp).  Python's lzma module does the same on the desktop;
 * the Android Python has no lzma module, so this is what makes CHD files with
 * LZMA hunks work there.
 *
 * lzma_decode_raw() returns the number of bytes written, or a negative error.
 */
#include <stddef.h>
#include <stdint.h>

#define PROB_BITS 11
#define PROB_INIT (1u << (PROB_BITS - 1))
#define NUM_STATES 12
#define POS_BITS_MAX 4
#define LEN_LOW 3
#define LEN_MID 3
#define LEN_HIGH 8
#define END_POS_MODEL 14
#define NUM_FULL_DIST (1 << (END_POS_MODEL >> 1))
#define ALIGN_BITS 4

typedef uint16_t prob;

typedef struct {
    const uint8_t *in, *end;
    uint32_t range, code;
    int err;
} rc_t;

static inline uint8_t rc_byte(rc_t *rc)
{
    if (rc->in >= rc->end) {
        rc->err = 1;
        return 0;
    }
    return *rc->in++;
}

static inline void rc_norm(rc_t *rc)
{
    if (rc->range < (1u << 24)) {
        rc->range <<= 8;
        rc->code = (rc->code << 8) | rc_byte(rc);
    }
}

static inline int rc_bit(rc_t *rc, prob *p)
{
    uint32_t bound = (rc->range >> PROB_BITS) * *p;
    int bit;
    if (rc->code < bound) {
        *p += ((1u << PROB_BITS) - *p) >> 5;
        rc->range = bound;
        bit = 0;
    } else {
        *p -= *p >> 5;
        rc->code -= bound;
        rc->range -= bound;
        bit = 1;
    }
    rc_norm(rc);
    return bit;
}

static uint32_t rc_direct(rc_t *rc, int n)
{
    uint32_t res = 0;
    while (n--) {
        rc->range >>= 1;
        rc->code -= rc->range;
        uint32_t t = 0u - (rc->code >> 31);
        rc->code += rc->range & t;
        rc_norm(rc);
        res = (res << 1) + (t + 1);
    }
    return res;
}

static uint32_t tree(rc_t *rc, prob *probs, int bits)
{
    uint32_t m = 1;
    for (int i = 0; i < bits; i++)
        m = (m << 1) + rc_bit(rc, &probs[m]);
    return m - (1u << bits);
}

static uint32_t tree_rev(rc_t *rc, prob *probs, int bits)
{
    uint32_t m = 1, sym = 0;
    for (int i = 0; i < bits; i++) {
        uint32_t bit = rc_bit(rc, &probs[m]);
        m = (m << 1) + bit;
        sym |= bit << i;
    }
    return sym;
}

typedef struct {
    prob choice, choice2;
    prob low[1 << POS_BITS_MAX][1 << LEN_LOW];
    prob mid[1 << POS_BITS_MAX][1 << LEN_MID];
    prob high[1 << LEN_HIGH];
} lenc_t;

static uint32_t len_decode(rc_t *rc, lenc_t *l, unsigned pos_state)
{
    if (!rc_bit(rc, &l->choice))
        return tree(rc, l->low[pos_state], LEN_LOW);
    if (!rc_bit(rc, &l->choice2))
        return 8 + tree(rc, l->mid[pos_state], LEN_MID);
    return 16 + tree(rc, l->high, LEN_HIGH);
}

static void init_probs(prob *p, size_t n)
{
    for (size_t i = 0; i < n; i++)
        p[i] = PROB_INIT;
}

#define LC 3
#define LP 0
#define PB 2

static struct {
    prob literal[0x300 << (LC + LP)];
    prob is_match[NUM_STATES << POS_BITS_MAX], is_rep[NUM_STATES], is_rep_g0[NUM_STATES],
        is_rep_g1[NUM_STATES], is_rep_g2[NUM_STATES], is_rep0_long[NUM_STATES << POS_BITS_MAX];
    prob pos_slot[4][1 << 6];
    prob pos[1 + NUM_FULL_DIST - END_POS_MODEL];
    prob align[1 << ALIGN_BITS];
    lenc_t len, rep_len;
} M;

long lzma_decode_raw(const uint8_t *src, long srclen, uint8_t *out, long outlen)
{
    rc_t rc = {src, src + srclen, 0xffffffffu, 0, 0};
    if (rc_byte(&rc) != 0)
        return -2;
    for (int i = 0; i < 4; i++)
        rc.code = (rc.code << 8) | rc_byte(&rc);
    if (rc.code == rc.range)
        return -2;
    init_probs((prob *)&M, sizeof M / sizeof(prob));
    uint32_t rep0 = 0, rep1 = 0, rep2 = 0, rep3 = 0;
    unsigned state = 0;
    long pos = 0;
    const unsigned pb_mask = (1u << PB) - 1;
    while (pos < outlen) {
        unsigned ps = (unsigned)pos & pb_mask;
        if (!rc_bit(&rc, &M.is_match[(state << POS_BITS_MAX) + ps])) {
            unsigned prev = pos ? out[pos - 1] : 0;
            prob *probs = &M.literal[0x300 * (((unsigned)pos & ((1u << LP) - 1)) << LC | (prev >> (8 - LC)))];
            unsigned sym = 1;
            if (state >= 7) {
                if ((long)rep0 >= pos)
                    return -3;
                unsigned match = out[pos - rep0 - 1];
                do {
                    unsigned mbit = (match >> 7) & 1;
                    match <<= 1;
                    unsigned bit = (unsigned)rc_bit(&rc, &probs[((1 + mbit) << 8) + sym]);
                    sym = (sym << 1) | bit;
                    if (mbit != bit)
                        break;
                } while (sym < 0x100);
            }
            while (sym < 0x100)
                sym = (sym << 1) | (unsigned)rc_bit(&rc, &probs[sym]);
            out[pos++] = (uint8_t)sym;
            state = state < 4 ? 0 : state < 10 ? state - 3 : state - 6;
            if (rc.err)
                return -4;
            continue;
        }
        uint32_t len;
        if (rc_bit(&rc, &M.is_rep[state])) {
            if (pos == 0)
                return -3;
            if (!rc_bit(&rc, &M.is_rep_g0[state])) {
                if (!rc_bit(&rc, &M.is_rep0_long[(state << POS_BITS_MAX) + ps])) {
                    state = state < 7 ? 9 : 11;
                    out[pos] = out[pos - rep0 - 1];
                    pos++;
                    continue;
                }
            } else {
                uint32_t dist;
                if (!rc_bit(&rc, &M.is_rep_g1[state])) {
                    dist = rep1;
                } else {
                    if (!rc_bit(&rc, &M.is_rep_g2[state])) {
                        dist = rep2;
                    } else {
                        dist = rep3;
                        rep3 = rep2;
                    }
                    rep2 = rep1;
                }
                rep1 = rep0;
                rep0 = dist;
            }
            len = len_decode(&rc, &M.rep_len, ps);
            state = state < 7 ? 8 : 11;
        } else {
            rep3 = rep2;
            rep2 = rep1;
            rep1 = rep0;
            len = len_decode(&rc, &M.len, ps);
            state = state < 7 ? 7 : 10;
            /* distance */
            unsigned len_state = len < 3 ? len : 3;
            uint32_t slot = tree(&rc, M.pos_slot[len_state], 6);
            if (slot < 4) {
                rep0 = slot;
            } else {
                int bits = (int)(slot >> 1) - 1;
                uint32_t dist = (2 | (slot & 1)) << bits;
                if (slot < END_POS_MODEL) {
                    dist += tree_rev(&rc, &M.pos[dist - slot], bits);
                } else {
                    dist += rc_direct(&rc, bits - ALIGN_BITS) << ALIGN_BITS;
                    dist += tree_rev(&rc, M.align, ALIGN_BITS);
                }
                rep0 = dist;
            }
            if (rep0 == 0xffffffffu)
                break;                          /* end marker */
            if ((long)rep0 >= pos)
                return -3;
        }
        len += 2;
        if (rc.err)
            return -4;
        for (uint32_t i = 0; i < len && pos < outlen; i++, pos++)
            out[pos] = out[pos - rep0 - 1];
    }
    return pos;
}

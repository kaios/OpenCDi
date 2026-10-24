/* Native SCC68070 core: run loop, exceptions, memory access.
 *
 * The instruction handlers and the small runtime helpers (flags, shifts,
 * BCD, exception entry) are generated from opencdi/m68k.py by
 * opencdi/m68k_native.py into ops.inc.  This file mirrors the hand-written
 * parts of RUNTIME in m68k.py: run(), bus_error() and brk(), plus the bus
 * functions of opencdi/bus.py.  Python exceptions are modelled with
 * setjmp/longjmp, keeping the same try/except structure as run() there, so
 * both cores behave the same, faults included.
 *
 * All values are int64_t (built with -fwrapv), like Python ints in the
 * ranges the handlers use.
 */
#include <setjmp.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef int64_t i64;
typedef struct { i64 a, b; } pair;
typedef struct { i64 x, y, z; const uint8_t *t; } opargs;
typedef i64 (*handler_fn)(i64 pc, const opargs *A);

/* shared with Python (m68k_native._State): every field is int64 */
typedef struct {
    i64 R[16];
    i64 ccr, srh, usp, ssp, PC, vbr, xc, ipl, nmi_edge, stopped, halted, FRAME_FORMAT, TRACE;
    i64 fault_addr, fault_write, fault_vector, abort_kind;
} cpu_state;

/* callbacks into Python: 0 = ok, 1 = BusError (fault_* set), 2 = IndexError, 3 = other exception */
typedef int (*io_r_fn)(i64 a, int size, i64 *out);
typedef int (*io_w_fn)(i64 a, int size, i64 v);
typedef int (*iack_fn)(i64 level, i64 *out);
typedef int (*void_fn)(void);
typedef int (*pcop_fn)(i64 pc, i64 op);

typedef struct {
    cpu_state *st;
    uint8_t **rp, **wp, **fp;      /* 65536 pages of 64 KiB each; NULL = device */
    io_r_fn ior;
    io_w_fn iow;
    iack_fn iack;
    void_fn reset;
    pcop_fn log_fault, log_trace;
} cpu_inst;

#define M32 INT64_C(0xffffffff)

/* the CPU being run; cpu_run() is not re-entrant */
static cpu_state *C;
static cpu_inst *I;

/* exceptions: what Python would raise, and where it would be caught */
enum { K_BREAK = 1, K_BUS = 2, K_INDEX = 3, K_PY = 4 };
enum { CTX_NONE, CTX_TRY, CTX_BUSERR };
static jmp_buf abort_jb, try_jb, be_jb, trace_jb;
static int ctx, trace_inner;

static void throw_kind(int kind) __attribute__((noreturn));
static void throw_kind(int kind)
{
    if (kind != K_PY) {
        if (ctx == CTX_TRY) {
            if (kind == K_BREAK && trace_inner)
                longjmp(trace_jb, 1);          /* the trace path's own "except CpuBreak" */
            longjmp(try_jb, kind);
        }
        if (ctx == CTX_BUSERR && kind == K_BUS)
            longjmp(be_jb, 1);                  /* bus_error(): "except BusError" around the pushes */
    }
    longjmp(abort_jb, kind);                    /* not caught: leaves run() as a Python exception */
}

static void throw_bus(i64 addr, int write, int vector) __attribute__((noreturn));
static void throw_bus(i64 addr, int write, int vector)
{
    C->fault_addr = addr;
    C->fault_write = write;
    C->fault_vector = vector;
    throw_kind(K_BUS);
}

static void cb_fail(int rc) __attribute__((noreturn));
static void cb_fail(int rc)
{
    throw_kind(rc == 1 ? K_BUS : rc == 2 ? K_INDEX : K_PY);
}

/* ----- Python integer semantics ---------------------------------------- */
static inline i64 shl64(i64 a, i64 n) { return n >= 64 ? 0 : (i64)((uint64_t)a << n); }
static inline i64 shr64(i64 a, i64 n) { return n >= 64 ? (a < 0 ? -1 : 0) : a >> n; }
static inline i64 floordiv64(i64 a, i64 b)
{
    if (b == 0)
        return 0;
    i64 q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0)))
        q--;
    return q;
}
static inline i64 floormod64(i64 a, i64 b)
{
    if (b == 0)
        return 0;
    i64 m = a % b;
    if (m != 0 && ((m < 0) != (b < 0)))
        m += b;
    return m;
}

/* ----- memory (opencdi/bus.py: Bus.cpu_functions) ---------------------- */
static inline uint8_t *page_of(uint8_t **tab, i64 a)
{
    uint64_t i = (uint64_t)a >> 16;
    if (i >= 65536)
        throw_kind(K_INDEX);                    /* Python: list index out of range */
    return tab[i];
}

static inline const uint8_t *fetch_page(i64 i)
{
    if ((uint64_t)i >= 65536)
        throw_kind(K_INDEX);
    return I->fp[i];
}

static i64 io_read(i64 a, int size)
{
    i64 v = 0;
    int rc = I->ior(a, size, &v);
    if (rc)
        cb_fail(rc);
    return v;
}

static void io_write(i64 a, int size, i64 v)
{
    int rc = I->iow(a, size, v);
    if (rc)
        cb_fail(rc);
}

static inline i64 mem_rb(i64 a)
{
    uint8_t *p = page_of(I->rp, a);
    if (p)
        return p[a & 0xffff];
    return io_read(a, 1);
}

static inline i64 mem_rw(i64 a)
{
    if (a & 1)
        throw_bus(a, 0, 3);
    uint8_t *p = page_of(I->rp, a);
    if (p) {
        i64 o = a & 0xffff;
        return (i64)p[o] << 8 | p[o + 1];
    }
    return io_read(a, 2);
}

static inline i64 mem_rl(i64 a)
{
    if (a & 1)
        throw_bus(a, 0, 3);
    uint8_t *p = page_of(I->rp, a);
    if (p) {
        i64 o = a & 0xffff;
        if (o < 0xfffd)
            return (i64)p[o] << 24 | (i64)p[o + 1] << 16 | (i64)p[o + 2] << 8 | p[o + 3];
    }
    return mem_rw(a) << 16 | mem_rw((a + 2) & M32);
}

static inline void mem_wb(i64 a, i64 v)
{
    uint8_t *p = page_of(I->wp, a);
    if (p)
        p[a & 0xffff] = (uint8_t)v;
    else
        io_write(a, 1, v);
}

static inline void mem_ww(i64 a, i64 v)
{
    if (a & 1)
        throw_bus(a, 1, 3);
    uint8_t *p = page_of(I->wp, a);
    if (p) {
        i64 o = a & 0xffff;
        p[o] = (uint8_t)(v >> 8);
        p[o + 1] = (uint8_t)v;
    } else {
        io_write(a, 2, v);
    }
}

static inline void mem_wl(i64 a, i64 v)
{
    if (a & 1)
        throw_bus(a, 1, 3);
    uint8_t *p = page_of(I->wp, a);
    if (p) {
        i64 o = a & 0xffff;
        if (o < 0xfffd) {
            p[o] = (uint8_t)(v >> 24);
            p[o + 1] = (uint8_t)(v >> 16);
            p[o + 2] = (uint8_t)(v >> 8);
            p[o + 3] = (uint8_t)v;
            return;
        }
    }
    mem_ww(a, v >> 16);
    mem_ww((a + 2) & M32, v & 0xffff);
}

/* ----- callbacks -------------------------------------------------------- */
static i64 cb_iack(i64 level)
{
    i64 v = 0;
    int rc = I->iack(level, &v);
    if (rc)
        cb_fail(rc);
    return v;
}

static void cb_reset_devices(void)
{
    int rc = I->reset();
    if (rc)
        cb_fail(rc);
}

static void cb_log(pcop_fn fn, i64 pc, i64 op)
{
    int rc = fn(pc, op);
    if (rc)
        cb_fail(rc);
}

/* brk(): the instruction completed and the run loop must look at SR/STOP */
static void cpu_brk(i64 pc) __attribute__((noreturn));
static void cpu_brk(i64 pc)
{
    C->PC = pc;
    throw_kind(K_BREAK);
}

#include "ops.inc"

/* ----- opcode tables ---------------------------------------------------- */
static handler_fn OPF[65536];
static opargs OPA[65536];
static int32_t CYC[65536];
static uint8_t COND_T[16][32];

int cpu_n_templates(void) { return N_TEMPLATES; }

int cpu_init_tables(const uint16_t *tid, const i64 *args, const int32_t *cycles, const uint8_t *cond)
{
    memcpy(COND_T, cond, sizeof COND_T);
    for (int op = 0; op < 65536; op++) {
        if (tid[op] >= N_TEMPLATES || args[4 * op + 3] < 0 || args[4 * op + 3] >= 16)
            return -1;
        OPF[op] = TEMPLATES[tid[op]];
        OPA[op].x = args[4 * op];
        OPA[op].y = args[4 * op + 1];
        OPA[op].z = args[4 * op + 2];
        OPA[op].t = COND_T[args[4 * op + 3]];
        CYC[op] = cycles[op];
    }
    return 0;
}

void *cpu_setup(cpu_state *st, uint8_t **rp, uint8_t **wp, uint8_t **fp, io_r_fn ior, io_w_fn iow,
                iack_fn iack, void_fn reset, pcop_fn log_fault, pcop_fn log_trace)
{
    cpu_inst *h = calloc(1, sizeof *h);
    if (!h)
        return 0;
    h->st = st;
    h->rp = rp;
    h->wp = wp;
    h->fp = fp;
    h->ior = ior;
    h->iow = iow;
    h->iack = iack;
    h->reset = reset;
    h->log_fault = log_fault;
    h->log_trace = log_trace;
    return h;
}

void cpu_free(void *h) { free(h); }

/* bus_error() of m68k.py: SCC68070 long format frame, 17 words, as MAME stacks it */
static i64 bus_error(i64 addr, i64 vector, i64 ppc, i64 ir)
{
    i64 sr = h_enter_exception();
    int saved = ctx;
    ctx = CTX_BUSERR;
    if (setjmp(be_jb)) {
        ctx = saved;
        C->halted = 1;
        return ppc;
    }
    C->R[15] = (C->R[15] - 2) & M32;           /* internal information */
    h_push16(0);                                /* instruction input buffer */
    h_push16(ir);                               /* instruction register */
    h_push32(0);                                /* data input buffer */
    h_push32(addr);                             /* fault address */
    h_push32(0);                                /* data output buffer */
    C->R[15] = (C->R[15] - 4) & M32;           /* internal information */
    h_push16(0);                                /* movem mask */
    h_push16(0);                                /* special status word */
    h_push16(0xf000 | (2 << 2));
    h_push32(ppc);
    h_push16(sr);
    ctx = saved;
    C->xc += 80;
    return mem_rl((C->vbr + (vector << 2)) & M32);
}

/* run() of m68k.py.  The loop state lives in statics so it survives longjmp. */
static i64 r_pc, r_left, r_ppc, r_op;

i64 cpu_run(void *handle, i64 budget)
{
    I = handle;
    C = I->st;
    int k = setjmp(abort_jb);
    if (k) {
        ctx = CTX_NONE;
        trace_inner = 0;
        C->abort_kind = k;
        return -1;
    }
    ctx = CTX_NONE;
    trace_inner = 0;
    r_pc = C->PC;
    r_left = budget;
    if (C->halted)
        return budget;
    if (h_irq_pending())
        r_pc = h_take_interrupt(r_pc);
    else if (C->stopped)
        return budget;
    r_op = 0;
    r_ppc = r_pc;
    while (r_left > 0) {
        ctx = CTX_TRY;
        int kind = setjmp(try_jb);
        if (kind == 0) {
            i64 pc = r_pc, left = r_left;
            if ((C->srh & 0x8000) && C->TRACE) {
                /* trace mode: one instruction, then a trace exception (vector 9) */
                r_ppc = pc;
                if (pc & 1)
                    throw_bus(pc, 0, 3);
                const uint8_t *p = fetch_page(pc >> 16);
                i64 o = pc & 0xffff;
                i64 op = (i64)p[o] << 8 | p[o + 1];
                r_op = op;
                trace_inner = 1;
                if (setjmp(trace_jb) == 0) {
                    pc = OPF[op](pc + 2, &OPA[op]);
                    trace_inner = 0;
                } else {
                    trace_inner = 0;
                    pc = C->PC;
                }
                r_pc = pc;
                left -= CYC[op];
                r_left = left;
                C->stopped = 0;
                pc = h_exception(9, pc);
                r_pc = pc;
                cb_log(I->log_trace, r_ppc, op);
                if (h_irq_pending()) {
                    pc = h_take_interrupt(pc);
                    r_pc = pc;
                }
                ctx = CTX_NONE;
                continue;
            }
            while (left > 0) {
                r_ppc = pc;
                r_pc = pc;
                r_left = left;
                if (pc & 1)
                    throw_bus(pc, 0, 3);
                const uint8_t *p = fetch_page(pc >> 16);
                i64 o = pc & 0xffff;
                i64 op = (i64)p[o] << 8 | p[o + 1];
                r_op = op;
                pc = OPF[op](pc + 2, &OPA[op]);
                left -= CYC[op];
            }
            r_pc = pc;
            r_left = left;
            ctx = CTX_NONE;
            continue;
        }
        /* an exception reached run()'s try */
        ctx = CTX_NONE;
        trace_inner = 0;
        if (kind == K_BREAK) {
            /* the instruction completed (it wrote SR or executed STOP): count it */
            r_left -= CYC[r_op];
            r_pc = C->PC;
            if ((C->srh & 0x8000) && C->TRACE)
                continue;
            if (h_irq_pending())
                r_pc = h_take_interrupt(r_pc);
            else if (C->stopped)
                break;
        } else if (kind == K_BUS) {
            r_pc = bus_error(C->fault_addr, C->fault_vector, r_ppc, r_op);
            if (C->halted)
                break;
        } else {
            /* IndexError: logged, then a guest bus error at pc */
            cb_log(I->log_fault, r_ppc, r_op);
            r_pc = bus_error(r_pc, 2, r_ppc, r_op);
            if (C->halted)
                break;
        }
    }
    C->PC = r_pc;
    i64 used = budget - r_left + C->xc;
    C->xc = 0;
    if (C->stopped || C->halted)
        return used > budget ? used : budget;
    return used;
}

/* Part of io_uring.c -- #included into that translation unit, not compiled
 * standalone.  See io_uring.c for the includes/typedefs/file-scope state
 * these definitions depend on. */

/* ---- Provided buffer ring (un)registration ----------------------------------
 * Both buffer rings register through here: the global ring's (TCPConn
 * multishot recv, io_uring_l_sys.c.inc) and each hub ring's
 * (STACKWEAVE_IOURING_MS, io_uring_l_loop.c.inc).
 *
 * Ubuntu's 6.8 kernels from 6.8.0-139 on carry a bad backport of upstream's
 * "io_uring/kbuf: use mem_is_zero()" that INVERTS the check of
 * io_uring_buf_reg's reserved words, in both IORING_REGISTER_PBUF_RING and
 * IORING_UNREGISTER_PBUF_RING: a call with zeroed resv -- what every correct
 * kernel requires -- gets EINVAL, and a call with a nonzero resv word goes
 * through (measured on 6.8.0-142-generic; netty/netty#17636 is the same bug).
 * So when a 6.8 kernel answers EINVAL, registration retries once with
 * resv[0] = 1.  A correct kernel answers any nonzero resv word with EINVAL, so
 * the retry can only succeed on a kernel with the inverted check, and changes
 * nothing anywhere else; the 6.8 gate also keeps a later kernel that gives
 * resv[0] a meaning from ever being sent it.  A kernel accepts exactly one of
 * the two forms, so once a registration needed the workaround, every later
 * call -- unregistration included -- sends resv[0] = 1 too.
 *
 * STACKWEAVE_IOURING_PBUF_RESV_QUIRK: unset or "auto" = the above; "0" = never
 * (on such a kernel the multishot paths then fall back to single-shot recv,
 * with their warning); "1" = always send resv[0] = 1, on any kernel -- a test
 * knob: on a correct kernel it makes every registration fail, which drives the
 * fallback paths. */
#define RUNLOOM_PBUF_QUIRK_AUTO  0
#define RUNLOOM_PBUF_QUIRK_OFF   1
#define RUNLOOM_PBUF_QUIRK_FORCE 2

static int runloom_iouring_pbuf_quirk_mode(void)
{
    static int v = -1;
    int cur = __atomic_load_n(&v, __ATOMIC_RELAXED);
    if (cur < 0) {
        const char *e = getenv("STACKWEAVE_IOURING_PBUF_RESV_QUIRK");
        cur = RUNLOOM_PBUF_QUIRK_AUTO;
        if (e != NULL && strcmp(e, "0") == 0) cur = RUNLOOM_PBUF_QUIRK_OFF;
        if (e != NULL && strcmp(e, "1") == 0) cur = RUNLOOM_PBUF_QUIRK_FORCE;
        __atomic_store_n(&v, cur, __ATOMIC_RELAXED);
    }
    return cur;
}

/* 1 once a buffer ring was registered with resv[0] = 1
 * (stats()["iouring_pbuf_resv_quirk"]). */
static int runloom_iouring_pbuf_resv_used = 0;

int runloom_iouring_pbuf_resv_quirk(void)
{
    return __atomic_load_n(&runloom_iouring_pbuf_resv_used, __ATOMIC_RELAXED);
}

/* Register *reg (its resv words are overwritten).  Returns 0, or -1 with errno
 * set to the kernel's refusal of the plain registration. */
static int runloom_iouring_pbuf_ring_register(int ring_fd,
                                              struct runloom_iouring_buf_reg *reg)
{
    static int warned = 0;
    int mode = runloom_iouring_pbuf_quirk_mode();
    struct utsname u;
    memset(reg->resv, 0, sizeof(reg->resv));
    if (mode == RUNLOOM_PBUF_QUIRK_FORCE ||
            __atomic_load_n(&runloom_iouring_pbuf_resv_used, __ATOMIC_RELAXED))
        reg->resv[0] = 1;
    if (sys_io_uring_register(ring_fd, IORING_REGISTER_PBUF_RING, reg, 1) == 0) {
        if (reg->resv[0] != 0)
            __atomic_store_n(&runloom_iouring_pbuf_resv_used, 1,
                             __ATOMIC_RELAXED);
        return 0;
    }
    if (errno != EINVAL || reg->resv[0] != 0 || mode != RUNLOOM_PBUF_QUIRK_AUTO
            || uname(&u) != 0 || strncmp(u.release, "6.8.", 4) != 0)
        return -1;
    reg->resv[0] = 1;
    if (sys_io_uring_register(ring_fd, IORING_REGISTER_PBUF_RING, reg, 1) != 0) {
        errno = EINVAL;   /* report the plain call's refusal, not the retry's */
        return -1;
    }
    __atomic_store_n(&runloom_iouring_pbuf_resv_used, 1, __ATOMIC_RELAXED);
    if (__atomic_exchange_n(&warned, 1, __ATOMIC_RELAXED) == 0)
        fprintf(stderr, "[stackweave] io_uring: this kernel (%s) refuses a "
                "provided buffer ring registration whose reserved words are "
                "zero -- an inverted check in Ubuntu's 6.8 kernels -- so "
                "buffer rings are registered with the workaround (resv[0] = 1) "
                "and multishot recv runs normally; "
                "STACKWEAVE_IOURING_PBUF_RESV_QUIRK=0 turns it off\n",
                u.release);
    return 0;
}

/* Unregister buffer group bgid.  Returns 0, or -1 with errno: the kernel still
 * holds the ring then and may still pick a buffer from it, so the caller must
 * not free the ring or its buffers. */
static int runloom_iouring_pbuf_ring_unregister(int ring_fd, uint16_t bgid)
{
    struct runloom_iouring_buf_reg reg;
    memset(&reg, 0, sizeof(reg));
    reg.bgid = bgid;
    if (__atomic_load_n(&runloom_iouring_pbuf_resv_used, __ATOMIC_RELAXED))
        reg.resv[0] = 1;
    return sys_io_uring_register(ring_fd, IORING_UNREGISTER_PBUF_RING,
                                 &reg, 1) == 0 ? 0 : -1;
}

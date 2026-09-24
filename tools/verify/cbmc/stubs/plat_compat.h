/* plat_compat.h -- CBMC stub.
 *
 * The real src/runloom_c/plat_compat.h pulls in the pthread threading
 * glue.  Under CBMC cldeque.c uses the genuine __atomic_* builtins
 * directly and needs nothing from this header.  This stub just
 * satisfies the `#include "plat_compat.h"` line so we verify the REAL
 * cldeque.c source (compiled unmodified) rather than a hand-copy.
 */
#ifndef RUNLOOM_PLAT_COMPAT_STUB_H
#define RUNLOOM_PLAT_COMPAT_STUB_H
#endif

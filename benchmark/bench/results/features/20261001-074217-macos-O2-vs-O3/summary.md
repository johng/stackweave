# stackweave feature matrix 20261001-074217-macos-O2-vs-O3

- host: M5-Max-Mar-2026, Apple M5 Max, 18 vCPU (6P+12E), python 3.14.4 (gil off)
- stackweave f5a9c255, netpoll kqueue, TLBC on
- builds: O2, O3
- 1-min load average across the runs: 4.2 .. 6.2
- 2 interleaved pass(es); cells are the median over passes; the delta is the median per-pass delta vs `O2/default`, marked ▲ (better) / ▼ (worse) only with 2+ passes that all agree on its sign and a size over 3%.
- Apple M5 Max laptop on battery (Low Power Mode off), run under caffeinate; macOS cannot pin hub threads, so a run that lands hubs on efficiency cores reads ~2.4x slower on ping-pong -- compare runs taken on a quiet machine
- O2 = the default build (setup.py's -O2 wins over the interpreter's -O3); O3 = the same commit built with STACKWEAVE_EXTRA_CFLAGS='-DPy_TSTATE_ALLOC_HOME -DPy_TSTATE_EXEC_HOME -O3', which comes last and wins
- every mnsched entry ran in its own fresh interpreter; io_uring configs (TCPCONN_IOURING, loop, loop+multishot) skip on macOS

## mnsched

| bench | O2/default | O3/default |
|---|---:|---:|
| pingpong local-wake | 2711311 | 2745107 (+1.3%) |
| pingpong same-hub pinned | 2568498 | 2569008 (+0.0%) |
| pingpong cross-hub pinned | 142761 | 139144 (-2.1%) |
| pingpong cross-hub pinned drifted | 7401 | 7398 (-0.0%) |
| pingpong cross-hub busy | 2202 | 2367 (+7.5% ▲) |
| spawn noop mn_fiber | 441878 | 445680 (+0.8%) |
| spawn noop stackweave.fiber | 394664 | 392501 (-0.5%) |
| yield 1000 fibers x200 | 13539081 | 13080427 (-3.4%) |
| 64 pairs pingpong | 7586784 | 9131747 (+21.6% ▲) |
| fan-out 1->32 buf64 | 2360132 | 2374555 (+0.8%) |
| mutex 64 fibers contended | 4646026 | 4605616 (-1.1%) |
| waitgroup fork-join 100x50 | 156748 | 155896 (-0.6%) |
| select 2 chans | 2721537 | 3065591 (+15.0%) |
| blocking() sleep(100us) | 22350 | 22302 (-0.2%) |
| 64 pairs pingpong @1h | 2939503 | 2956830 (+0.6%) |
| 64 pairs pingpong @2h | 4564282 | 4571347 (+6.5%) |
| 64 pairs pingpong @4h | 7599364 | 8816356 (+16.7% ▲) |
| 64 pairs pingpong @8h | 10051342 | 9609338 (-4.0%) |
| foreign thread -> fiber wake [p50] | 2.6 us | 2.6 us (+0.8%) |
| foreign thread -> fiber wake [p99] | 11.7 us | 11.4 us (-2.5%) |
| cross-hub pinned wake [p50] | 3.1 us | 3.1 us (-2.5%) |
| cross-hub pinned wake [p99] | 11.1 us | 12.0 us (+8.3% ▼) |
| spawn->run own hub [p50] | 4.5 us | 4.7 us (+3.5%) |
| spawn->run own hub [p99] | 8.8 us | 8.1 us (-7.1% ▲) |
| spawn->run remote idle hub [p50] | 257.7 us | 221.1 us (-14.0% ▲) |
| spawn->run remote idle hub [p99] | 429.5 us | 433.4 us (+0.9%) |
| spawn->run round-robin [p50] | 223.4 us | 232.0 us (+3.9% ▼) |
| spawn->run round-robin [p99] | 637.9 us | 638.6 us (+0.1%) |
| timer lateness 1ms [p50] | 7.3 us | 7.3 us (-0.9%) |
| timer lateness 1ms [p99] | 30.1 us | 29.8 us (-1.3%) |
| spawn->run remote idle hub, after fork-join load [p50] | 176019.6 us | 186699.9 us (+6.1% ▼) |
| spawn->run remote idle hub, after fork-join load [p99] | 343947.3 us | 350283.4 us (+0.7%) |

## echo

| bench | O2/default | O3/default |
|---|---:|---:|
| c-echo @2h | 72586 | 72560 (-0.0%) |
| py-echo @2h | 148434 | 149681 (+0.8%) |
| c-echo @4h | 80301 | 90902 (+15.2%) |
| py-echo @4h | 163623 | 173315 (+6.3%) |
| c-echo @8h | 52334 | 53758 (+2.8%) |
| py-echo @8h | 120603 | 122353 (+1.5%) |


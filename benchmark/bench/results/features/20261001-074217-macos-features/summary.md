# stackweave feature matrix 20261001-074217-macos-features

- host: M5-Max-Mar-2026, Apple M5 Max, 18 vCPU (6P+12E), python 3.14.4 (gil off)
- stackweave f5a9c255, netpoll kqueue, TLBC on
- builds: cur
- 1-min load average across the runs: 2.0 .. 6.8
- 3 interleaved pass(es); cells are the median over passes; the delta is the median per-pass delta vs `default`, marked ▲ (better) / ▼ (worse) only with 2+ passes that all agree on its sign and a size over 3%.
- Apple M5 Max laptop on battery (Low Power Mode off), run under caffeinate; macOS cannot pin hub threads, so a run that lands hubs on efficiency cores reads ~2.4x slower on ping-pong -- compare runs taken on a quiet machine
- every mnsched entry ran in its own fresh interpreter; io_uring configs (TCPCONN_IOURING, loop, loop+multishot) skip on macOS

## mnsched

| bench | default | stack-arena | optimize-throughput |
|---|---:|---:|---:|
| pingpong local-wake | 2770667 | 2743432 (-0.2%) | 2722196 (-1.7%) |
| pingpong same-hub pinned | 2566774 | 2570600 (+0.1%) | 2566317 (+0.0%) |
| pingpong cross-hub pinned | 129337 | 128231 (-1.0%) | 129193 (-0.5%) |
| pingpong cross-hub pinned drifted | 7389 | 7394 (-0.1%) | 7384 (-0.1%) |
| pingpong cross-hub busy | 2224 | 2209 (-1.2%) | 2337 (+3.8% ▲) |
| spawn noop mn_fiber | 433194 | 447169 (+2.1%) | 461156 (+5.3% ▲) |
| spawn noop stackweave.fiber | 387990 | 280878 (-28.6% ▼) | 389231 (+0.4%) |
| yield 1000 fibers x200 | 13534375 | 13659841 (+0.9%) | 13620847 (+0.7%) |
| 64 pairs pingpong | 8814403 | 8239990 (-13.0% ▼) | 8863333 (+0.7%) |
| fan-out 1->32 buf64 | 2300072 | 2316100 (+2.0%) | 2289121 (-2.6%) |
| mutex 64 fibers contended | 3955447 | 4884603 (+23.9%) | 5005454 (+22.5% ▲) |
| waitgroup fork-join 100x50 | 155432 | 163406 (+5.3% ▲) | 170762 (+9.9% ▲) |
| select 2 chans | 3026993 | 3072794 (+1.7%) | 3030854 (+0.1%) |
| blocking() sleep(100us) | 22277 | 22253 (-0.1%) | 29650 (+33.1% ▲) |
| 64 pairs pingpong @1h | 2981659 | 2981683 (-0.0%) | 2980465 (-0.4%) |
| 64 pairs pingpong @2h | 5540206 | 5581177 (+0.2%) | 5401827 (-0.2%) |
| 64 pairs pingpong @4h | 9697613 | 8249072 (-15.0% ▼) | 8872625 (-6.8%) |
| 64 pairs pingpong @8h | 9600180 | 9525892 (-4.8% ▼) | 9210455 (-3.7% ▼) |
| foreign thread -> fiber wake [p50] | 2.6 us | 2.6 us (+0.0%) | 2.6 us (+0.0%) |
| foreign thread -> fiber wake [p99] | 12.0 us | 12.6 us (+7.1%) | 12.2 us (+13.2%) |
| cross-hub pinned wake [p50] | 3.0 us | 3.0 us (-1.4%) | 3.1 us (+5.6%) |
| cross-hub pinned wake [p99] | 16.2 us | 12.8 us (-18.4%) | 10.5 us (-21.1% ▲) |
| spawn->run own hub [p50] | 4.5 us | 4.5 us (-0.8%) | 4.5 us (-4.8%) |
| spawn->run own hub [p99] | 9.2 us | 8.4 us (-4.9% ▲) | 8.8 us (-1.9%) |
| spawn->run remote idle hub [p50] | 213.8 us | 216.7 us (+1.5%) | 213.9 us (+0.0%) |
| spawn->run remote idle hub [p99] | 432.2 us | 437.0 us (+1.1%) | 437.5 us (+1.2%) |
| spawn->run round-robin [p50] | 250.1 us | 223.7 us (-8.9% ▲) | 239.2 us (-1.8%) |
| spawn->run round-robin [p99] | 638.4 us | 638.0 us (+0.0%) | 637.0 us (-0.2%) |
| timer lateness 1ms [p50] | 7.4 us | 7.4 us (+3.5%) | 7.1 us (-3.4%) |
| timer lateness 1ms [p99] | 33.0 us | 26.9 us (-7.2%) | 31.0 us (+7.0%) |
| spawn->run remote idle hub, after fork-join load [p50] | 132903.0 us | 297486.7 us (+123.8% ▼) | 70.6 us (-99.9% ▲) |
| spawn->run remote idle hub, after fork-join load [p99] | 248217.2 us | 568339.5 us (+129.0% ▼) | 267.2 us (-99.9% ▲) |

## echo

| bench | default | stack-arena | optimize-throughput |
|---|---:|---:|---:|
| c-echo @2h | 72523 | 72721 (+0.3%) | 72816 (+0.6%) |
| py-echo @2h | 149027 | 150319 (+0.2%) | 149488 (+0.2%) |
| c-echo @4h | 91522 | 91644 (-0.0%) | 91204 (-0.2%) |
| py-echo @4h | 174149 | 174834 (+0.0%) | 174565 (+0.6%) |
| c-echo @8h | 53911 | 54566 (-0.0%) | 54357 (+0.7%) |
| py-echo @8h | 123667 | 123251 (-0.1%) | 123532 (-0.5%) |


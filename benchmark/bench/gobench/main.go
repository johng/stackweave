// Go on the bench.mnsched / bench.echo workloads.
//
// The same workloads, entry names and inner counts as the Python suites, so
// bench.compare puts Go in the same table.  Writes the harness's JSON shape
// ({"suite", "env", "results", "latency"}), with ops_per_s from the median of
// the samples.  GOMAXPROCS stands in for the hub count: STACKWEAVE_BENCH_HUBS
// (default 4) for the main entries, and N for the "@Nh" scaling and echo rows.
//
// Not here: stackweave's pinned / busy / drifted routing variants and a
// foreign-OS-thread wake (Go has no non-runtime threads without cgo).
//
//	go build -o gobench . && ./gobench -out go.json [-quick] [-suite mnsched|echo|all]
package main

import (
	"bytes"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"net"
	"os"
	"os/exec"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

type result map[string]any

var (
	samples, warmup int
	results         = []result{}
	latencies       = []result{}
)

func median(xs []float64) float64 {
	s := append([]float64(nil), xs...)
	sort.Float64s(s)
	n := len(s)
	if n%2 == 1 {
		return s[n/2]
	}
	return (s[n/2-1] + s[n/2]) / 2
}

// record mirrors bench.harness.summarize for one entry's per-sample seconds.
func record(name string, times []float64, inner int, note string) result {
	med := median(times)
	mean, mn := 0.0, math.Inf(1)
	for _, t := range times {
		mean += t
		mn = math.Min(mn, t)
	}
	mean /= float64(len(times))
	dev := make([]float64, len(times))
	sd := 0.0
	for i, t := range times {
		dev[i] = math.Abs(t - med)
		sd += (t - mean) * (t - mean)
	}
	sd = math.Sqrt(sd / float64(len(times)))
	r := result{
		"name": name, "note": note, "samples": len(times), "inner": inner,
		"median_s": med, "min_s": mn, "mean_s": mean, "mad_s": median(dev),
		"stdev_s": sd, "rsd_pct": sd / mean * 100, "per_op_s": med / float64(inner),
		"per_op_ns": med / float64(inner) * 1e9, "ops_per_s": float64(inner) / med,
	}
	results = append(results, r)
	fmt.Printf("  %-34s %10.1f ops/s  %10.1f ns/op  med=%8.3fms  rsd=%4.1f%%\n",
		name, r["ops_per_s"], r["per_op_ns"], med*1e3, r["rsd_pct"])
	return r
}

func bench(name string, inner int, note string, fn func()) []float64 {
	for i := 0; i < warmup; i++ {
		fn()
	}
	times := make([]float64, 0, samples)
	for i := 0; i < samples; i++ {
		runtime.GC() // untimed, like the harness's gc.collect()
		t0 := time.Now()
		fn()
		times = append(times, time.Since(t0).Seconds())
	}
	record(name, times, inner, note)
	return times
}

func latency(name string, ns []int64, note string) {
	sort.Slice(ns, func(i, j int) bool { return ns[i] < ns[j] })
	pct := func(p float64) float64 {
		i := int(float64(len(ns)) * p)
		if i >= len(ns) {
			i = len(ns) - 1
		}
		return float64(ns[i]) / 1e3
	}
	sum := 0.0
	for _, v := range ns {
		sum += float64(v)
	}
	r := result{"name": name, "note": note, "count": len(ns), "p50_us": pct(.5),
		"p90_us": pct(.9), "p99_us": pct(.99), "p999_us": pct(.999),
		"max_us": float64(ns[len(ns)-1]) / 1e3, "mean_us": sum / float64(len(ns)) / 1e3}
	latencies = append(latencies, r)
	fmt.Printf("  %-34s p50=%8.1fus  p90=%8.1fus  p99=%8.1fus  max=%9.1fus  n=%d\n",
		name, r["p50_us"], r["p90_us"], r["p99_us"], r["max_us"], len(ns))
}

func check(ok bool, what string) {
	if !ok {
		panic(what)
	}
}

// ---------------------------------------------------------------- workloads
func pairs(p, n int) func() {
	return func() {
		var wg sync.WaitGroup
		done := make([]byte, p)
		for k := 0; k < p; k++ {
			a, b := make(chan int), make(chan int)
			wg.Add(2)
			go func() {
				defer wg.Done()
				for i := 0; i < n; i++ {
					a <- i
					<-b
				}
			}()
			go func(k int) {
				defer wg.Done()
				for i := 0; i < n; i++ {
					b <- <-a
				}
				done[k] = 1
			}(k)
		}
		wg.Wait()
		check(bytes.Count(done, []byte{1}) == p, "pairs")
	}
}

func spawn(n int) func() {
	return func() {
		var wg sync.WaitGroup
		wg.Add(n)
		for i := 0; i < n; i++ {
			go wg.Done()
		}
		wg.Wait()
	}
}

func yield(units, m int) func() {
	return func() {
		var wg sync.WaitGroup
		count := make([]byte, units)
		wg.Add(units)
		for k := 0; k < units; k++ {
			go func(k int) {
				defer wg.Done()
				for i := 0; i < m; i++ {
					runtime.Gosched()
				}
				count[k] = 1
			}(k)
		}
		wg.Wait()
		check(bytes.Count(count, []byte{1}) == units, "yield")
	}
}

func fanout(items, workers, capacity int) func() {
	return func() {
		work, acks := make(chan int, capacity), make(chan int, capacity)
		var wg sync.WaitGroup
		go func() {
			for i := 0; i < items; i++ {
				work <- i
			}
			close(work)
		}()
		wg.Add(workers)
		for w := 0; w < workers; w++ {
			go func() {
				defer wg.Done()
				for v := range work {
					acks <- v
				}
			}()
		}
		s := 0
		for i := 0; i < items; i++ {
			s += <-acks
		}
		wg.Wait()
		check(s == items*(items-1)/2, "fanout")
	}
}

func mutex(units, m int) func() {
	return func() {
		var mu sync.Mutex
		var wg sync.WaitGroup
		cnt := 0
		wg.Add(units)
		for k := 0; k < units; k++ {
			go func() {
				defer wg.Done()
				for i := 0; i < m; i++ {
					mu.Lock()
					cnt++
					mu.Unlock()
				}
			}()
		}
		wg.Wait()
		check(cnt == units*m, "mutex")
	}
}

func waitgroup(rounds, width int) func() {
	return func() {
		hit := make([]byte, width)
		ok := 0
		for r := 0; r < rounds; r++ {
			var wg sync.WaitGroup
			wg.Add(width)
			for k := 0; k < width; k++ {
				go func(k int) { hit[k] = 1; wg.Done() }(k)
			}
			wg.Wait()
			ok += bytes.Count(hit, []byte{1})
			for k := range hit {
				hit[k] = 0
			}
		}
		check(ok == rounds*width, "waitgroup")
	}
}

func selectBench(n int) func() {
	return func() {
		a, b := make(chan int), make(chan int)
		half := n / 2
		for _, ch := range []chan int{a, b} {
			go func(ch chan int) {
				for i := 0; i < half; i++ {
					ch <- i
				}
			}(ch)
		}
		got := 0
		for got < 2*half {
			select {
			case <-a:
			case <-b:
			}
			got++
		}
	}
}

// time.Sleep parks on a runtime timer, so this is Go's natural way to wait
// 100us, not a blocking syscall: macOS select(2)/nanosleep round a 100us
// timeout up to ~1 ms, which would measure the kernel's timer slop instead.
func blockingCall() int {
	time.Sleep(100 * time.Microsecond)
	return 1
}

func blocking(callers, m int) func() {
	return func() {
		per := make([]int, callers)
		var wg sync.WaitGroup
		wg.Add(callers)
		for k := 0; k < callers; k++ {
			go func(k int) {
				defer wg.Done()
				for i := 0; i < m; i++ {
					per[k] += blockingCall()
				}
			}(k)
		}
		wg.Wait()
		s := 0
		for _, v := range per {
			s += v
		}
		check(s == callers*m, "blocking")
	}
}

func latWake(n int, gap time.Duration) []int64 {
	ch := make(chan time.Time) // monotonic; UnixNano is microsecond-grained on macOS
	lat := make([]int64, 0, n)
	done := make(chan struct{})
	go func() {
		for i := 0; i < n; i++ {
			v := <-ch
			lat = append(lat, int64(time.Since(v)))
		}
		close(done)
	}()
	for i := 0; i < n; i++ {
		ch <- time.Now()
		time.Sleep(gap)
	}
	<-done
	return lat
}

func latSpawn(n int, gap time.Duration) []int64 {
	lat := make([]int64, n)
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		t0 := time.Now()
		go func(i int) { lat[i] = int64(time.Since(t0)); wg.Done() }(i)
		time.Sleep(gap)
	}
	wg.Wait()
	return lat
}

func latTimer(units, rounds int, d time.Duration) []int64 {
	lat := make([]int64, units*rounds)
	var wg sync.WaitGroup
	wg.Add(units)
	for k := 0; k < units; k++ {
		go func(k int) {
			defer wg.Done()
			for r := 0; r < rounds; r++ {
				t0 := time.Now()
				time.Sleep(d)
				lat[k*rounds+r] = max(0, int64(time.Since(t0)-d))
			}
		}(k)
	}
	wg.Wait()
	return lat
}

func echo(procs, conns, rounds int) {
	runtime.GOMAXPROCS(procs)
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				buf := make([]byte, 4096)
				for {
					k, err := c.Read(buf)
					if err != nil {
						return
					}
					if _, err := c.Write(buf[:k]); err != nil {
						return
					}
				}
			}(c)
		}
	}()
	cs := make([]net.Conn, conns)
	for i := range cs {
		if cs[i], err = net.Dial("tcp", ln.Addr().String()); err != nil {
			panic(err)
		}
	}
	payload := make([]byte, 64)
	for i := range payload {
		payload[i] = byte(i)
	}
	once := func() {
		var wg sync.WaitGroup
		wg.Add(conns)
		for _, c := range cs {
			go func(c net.Conn) {
				defer wg.Done()
				buf := make([]byte, 64)
				for r := 0; r < rounds; r++ {
					if _, err := c.Write(payload); err != nil {
						panic(err)
					}
					if _, err := io.ReadFull(c, buf); err != nil {
						panic(err)
					}
					check(bytes.Equal(buf, payload), "echo mismatch")
				}
			}(c)
		}
		wg.Wait()
	}
	name := fmt.Sprintf("@%dh", procs)
	note := fmt.Sprintf("%d conns x %d x 64B, GOMAXPROCS=%d; same run in both rows", conns, rounds, procs)
	t := bench("c-echo "+name, conns*rounds, note, once)
	record("py-echo "+name, t, conns*rounds, note)
	for _, c := range cs {
		c.Close()
	}
	ln.Close()
}

// ---------------------------------------------------------------- main
func ints(s string) []int {
	var out []int
	for _, f := range strings.Split(s, ",") {
		if v, err := strconv.Atoi(strings.TrimSpace(f)); err == nil {
			out = append(out, v)
		}
	}
	return out
}

func sh(name string, args ...string) string {
	out, err := exec.Command(name, args...).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func main() {
	hubs := 4
	if v, err := strconv.Atoi(os.Getenv("STACKWEAVE_BENCH_HUBS")); err == nil && v > 0 {
		hubs = v
	}
	quick := flag.Bool("quick", false, "3 samples, smaller inner counts")
	suite := flag.String("suite", "all", "mnsched|echo|all")
	out := flag.String("out", "go.json", "result JSON path")
	scale := flag.String("scale", "1,2,4,8,16", "GOMAXPROCS for the 64-pair scaling rows")
	echoProcs := flag.String("echo-procs", "2,4,8,16", "GOMAXPROCS for the echo rows")
	flag.Parse()
	q := 1
	samples, warmup = 12, 3
	n := 4000
	if *quick {
		q, samples, warmup, n = 10, 3, 1, 400
	}
	ncpu := runtime.NumCPU()
	fmt.Printf("runtime: go %s  GOMAXPROCS=%d (scaling %s)\n\n", runtime.Version(), hubs, *scale)

	if *suite == "mnsched" || *suite == "all" {
		runtime.GOMAXPROCS(hubs)
		bench("pingpong local-wake", 100000/q, "one unbuffered pair", pairs(1, 100000/q))
		t := bench("spawn noop mn_fiber", 20000/q, "go f() + WaitGroup", spawn(20000/q))
		record("spawn noop stackweave.fiber", t, 20000/q, "same run as the row above")
		bench("yield 1000 fibers x200", 1000*(200/q), "runtime.Gosched", yield(1000, 200/q))
		bench("64 pairs pingpong", 64*(2000/q), "", pairs(64, 2000/q))
		bench("fan-out 1->32 buf64", 100000/q, "", fanout(100000/q, 32, 64))
		bench("mutex 64 fibers contended", 64*(2000/q), "sync.Mutex", mutex(64, 2000/q))
		wg := 50 / min(q, 5)
		bench("waitgroup fork-join 100x50", wg*100, "", waitgroup(wg, 100))
		bench("select 2 chans", 50000/q, "", selectBench(50000/q))
		bench("blocking() sleep(100us)", 32*(100/q), "time.Sleep(100us): a runtime timer, no syscall", blocking(32, 100/q))
		for _, h := range ints(*scale) {
			if h > ncpu {
				continue
			}
			runtime.GOMAXPROCS(h)
			bench(fmt.Sprintf("64 pairs pingpong @%dh", h), 64*(2000/q),
				fmt.Sprintf("GOMAXPROCS=%d", h), pairs(64, 2000/q))
		}
		runtime.GOMAXPROCS(hubs)
		latency("cross-hub pinned wake", latWake(n, 200*time.Microsecond), "goroutine -> parked goroutine")
		latency("spawn->run round-robin", latSpawn(n/4, 300*time.Microsecond), "go f() to first run")
		units := 1000
		if *quick {
			units = 200
		}
		latency("timer lateness 1ms", latTimer(units, 10, time.Millisecond), "time.Sleep(1ms) - 1ms")
	}
	if *suite == "echo" || *suite == "all" {
		rounds := max(10, 500/q)
		for _, h := range ints(*echoProcs) {
			if h <= ncpu {
				echo(h, 64, rounds)
			}
		}
	}

	host, _ := os.Hostname()
	load := strings.Fields(strings.Trim(sh("sysctl", "-n", "vm.loadavg"), "{ }"))
	if len(load) == 0 {
		if b, err := os.ReadFile("/proc/loadavg"); err == nil {
			load = strings.Fields(string(b))[:3]
		}
	}
	doc := map[string]any{
		"suite": "go",
		"env": map[string]any{
			"timestamp": time.Now().UTC().Format(time.RFC3339), "host": host,
			"nproc": ncpu, "runtime": "go", "go": runtime.Version(),
			"gomaxprocs": hubs, "loadavg": load,
		},
		"results": results, "latency": latencies,
	}
	f, err := os.Create(*out)
	if err != nil {
		panic(err)
	}
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	if err := enc.Encode(doc); err != nil {
		panic(err)
	}
	f.Close()
	fmt.Printf("\nwrote %s\n", *out)
}

// The application-shaped workloads (bench.apps) and the memory probe
// (bench.memory) in Go.  Constants mirror benchmark/bench/appspec.py -- keep
// them in step.  Written the way Go is normally used: net/http for the API,
// a goroutine per backend call / task / subscriber, channels in between.
package main

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	httpConns, httpReqs               = 64, 200
	gwRequests, gwInflight, gwFanout  = 2000, 100, 8
	gwLatency                         = time.Millisecond
	pipeRecords, pipeWorkers, pipeCap = 20000, 4, 256
	pubSubs, pubMsgs, pubCap          = 256, 500, 16
	crawlTasks, crawlFetches          = 10000, 3
	rowHTTP                           = "http json api 64 conns"
	rowGateway                        = "api gateway fan-out 8 x 1ms"
	rowPipeline                       = "parse+hash pipeline 4 workers"
	rowPubSub                         = "pub/sub broadcast 256 subs"
	rowCrawl                          = "crawler 10k concurrent x 3 fetches"
)

var memories = []result{}

func join(n int, f func(i int)) {
	var wg sync.WaitGroup
	wg.Add(n)
	for i := 0; i < n; i++ {
		go func(i int) { defer wg.Done(); f(i) }(i)
	}
	wg.Wait()
}

// ---------------------------------------------------------------- http
type user struct {
	ID    int      `json:"id"`
	Name  string   `json:"name"`
	Tags  []string `json:"tags"`
	Score int      `json:"score"`
}

func httpBench(nReqs int) {
	mux := http.NewServeMux()
	mux.HandleFunc("/user/", func(w http.ResponseWriter, r *http.Request) {
		uid, err := strconv.Atoi(strings.TrimPrefix(r.URL.Path, "/user/"))
		if err != nil || r.Method != http.MethodGet {
			http.NotFound(w, r)
			return
		}
		body, _ := json.Marshal(user{uid, fmt.Sprintf("user-%d", uid), []string{"a", "b", "c"}, uid * 7 % 1000})
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Content-Length", strconv.Itoa(len(body)))
		w.Write(body)
	})
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		panic(err)
	}
	srv := &http.Server{Handler: mux}
	go srv.Serve(ln)
	tr := &http.Transport{MaxIdleConns: httpConns, MaxIdleConnsPerHost: httpConns, MaxConnsPerHost: httpConns}
	client := &http.Client{Transport: tr}
	base := "http://" + ln.Addr().String() + "/user/"
	bench(rowHTTP, httpConns*nReqs, "net/http server + 64 client goroutines, keep-alive", func() {
		join(httpConns, func(ci int) {
			for i := 0; i < nReqs; i++ {
				uid := ci*100000 + i
				resp, err := client.Get(base + strconv.Itoa(uid))
				if err != nil {
					panic(err)
				}
				b, _ := io.ReadAll(resp.Body)
				resp.Body.Close()
				var u user
				check(resp.StatusCode == 200 && json.Unmarshal(b, &u) == nil && u.ID == uid, "http id")
			}
		})
	})
	tr.CloseIdleConnections()
	srv.Close()
}

// ---------------------------------------------------------------- gateway
func backendWork(req, k int) int { return 4950 + req*gwFanout + k } // sum(range(100)) + ...

func gateway(n int) func() {
	want := 0
	for r := 0; r < n; r++ {
		for k := 0; k < gwFanout; k++ {
			want += backendWork(r, k)
		}
	}
	return func() {
		res := make([]int, n*gwFanout)
		join(gwInflight, func(w int) {
			for r := w; r < n; r += gwInflight {
				join(gwFanout, func(k int) {
					time.Sleep(gwLatency)
					s := 0
					for i := 0; i < 100; i++ {
						s += i
					}
					res[r*gwFanout+k] = s + r*gwFanout + k
				})
			}
		})
		got := 0
		for _, v := range res {
			got += v
		}
		check(got == want, "gateway checksum")
	}
}

// ---------------------------------------------------------------- pipeline
type pipeRecord struct {
	ID      int    `json:"id"`
	User    string `json:"user"`
	Payload string `json:"payload"`
	Values  []int  `json:"values"`
}

func pipeWork(rec []byte) int {
	var d pipeRecord
	if err := json.Unmarshal(rec, &d); err != nil {
		panic(err)
	}
	h := sha256.Sum256([]byte(d.User + d.Payload))
	s := int(h[0])
	for _, v := range d.Values {
		s += v
	}
	return s
}

func pipeline(n int) func() {
	recs := make([][]byte, n)
	want := 0
	for i := range recs {
		vals := make([]int, 10)
		for j := range vals {
			vals[j] = i%50 + j
		}
		recs[i], _ = json.Marshal(pipeRecord{i, fmt.Sprintf("u%d", i), strings.Repeat("x", 200), vals})
		want += pipeWork(recs[i])
	}
	return func() {
		work, out := make(chan []byte, pipeCap), make(chan int, pipeCap)
		go func() {
			for _, r := range recs {
				work <- r
			}
			close(work)
		}()
		var wg sync.WaitGroup
		wg.Add(pipeWorkers)
		for w := 0; w < pipeWorkers; w++ {
			go func() {
				defer wg.Done()
				for r := range work {
					out <- pipeWork(r)
				}
			}()
		}
		go func() { wg.Wait(); close(out) }()
		total := 0
		for v := range out {
			total += v
		}
		check(total == want, "pipeline checksum")
	}
}

// ---------------------------------------------------------------- pub/sub
func pubsub(msgs int) func() {
	return func() {
		subs := make([]chan int, pubSubs)
		for i := range subs {
			subs[i] = make(chan int, pubCap)
		}
		got := make([]int, pubSubs)
		var wg sync.WaitGroup
		wg.Add(pubSubs)
		for k := range subs {
			go func(k int) {
				defer wg.Done()
				for v := range subs[k] {
					got[k] += v
				}
			}(k)
		}
		for i := 0; i < msgs; i++ {
			for _, ch := range subs {
				ch <- i
			}
		}
		for _, ch := range subs {
			close(ch)
		}
		wg.Wait()
		for _, g := range got {
			check(g == msgs*(msgs-1)/2, "pub/sub lost a message")
		}
	}
}

// ---------------------------------------------------------------- crawler
func crawl(n int) func() {
	want := 0
	for t := 0; t < n; t++ {
		for f := 0; f < crawlFetches; f++ {
			want += len(strconv.Itoa(t*31 + f))
		}
	}
	return func() {
		acc := make([]int, n)
		join(n, func(t int) {
			for f := 0; f < crawlFetches; f++ {
				time.Sleep(time.Duration(1+(t*7+f)%5) * time.Millisecond)
				acc[t] += len(strconv.Itoa(t*31 + f))
			}
		})
		got := 0
		for _, v := range acc {
			got += v
		}
		check(got == want, "crawl checksum")
	}
}

func runApps(quick bool, hubs int) {
	q := 1
	if quick {
		q = 10
	} else {
		samples, warmup = 10, 2
	}
	runtime.GOMAXPROCS(hubs)
	httpBench(max(10, httpReqs/q))
	bench(rowGateway, gwRequests/q, "goroutine per backend call + WaitGroup", gateway(gwRequests/q))
	bench(rowPipeline, pipeRecords/q, "chan(256) -> 4 worker goroutines", pipeline(pipeRecords/q))
	bench(rowPubSub, pubMsgs/q*pubSubs, "a goroutine + chan(16) per subscriber", pubsub(pubMsgs/q))
	bench(rowCrawl, crawlTasks/q*crawlFetches, "a goroutine per task", crawl(crawlTasks/q))
}

// ---------------------------------------------------------------- memory
func rssBytes() int64 {
	if b, err := os.ReadFile("/proc/self/statm"); err == nil {
		f := strings.Fields(string(b))
		pages, _ := strconv.ParseInt(f[1], 10, 64)
		return pages * int64(os.Getpagesize())
	}
	out, _ := exec.Command("ps", "-o", "rss=", "-p", strconv.Itoa(os.Getpid())).Output()
	kb, _ := strconv.ParseInt(strings.TrimSpace(string(out)), 10, 64)
	return kb * 1024
}

func runMemory(quick bool, hubs int) {
	runtime.GOMAXPROCS(hubs)
	for _, n := range []int{10000, 100000} {
		if quick {
			n /= 10
		}
		ch := make(chan struct{})
		var ready, done sync.WaitGroup
		ready.Add(n)
		done.Add(n)
		runtime.GC()
		before := rssBytes()
		for i := 0; i < n; i++ {
			go func() { ready.Done(); <-ch; done.Done() }()
		}
		ready.Wait()
		time.Sleep(200 * time.Millisecond)
		after := rssBytes()
		name := fmt.Sprintf("rss per parked unit @%dk", n/1000)
		per := float64(after-before) / float64(n)
		memories = append(memories, result{"name": name, "units": n, "rss_before": before,
			"rss_after": after, "bytes_per_unit": per, "note": "goroutines parked on a channel receive"})
		fmt.Printf("  %-34s %8.2f KB/unit  (%d units, RSS %.1f -> %.1f MB)\n",
			name, per/1024, n, float64(before)/(1<<20), float64(after)/(1<<20))
		close(ch)
		done.Wait()
	}
}

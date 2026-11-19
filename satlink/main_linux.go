package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"math"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

func replay(ctx context.Context, ports []*port, points []point, startArg string, limit uint32, hash string, tolerance time.Duration) (err error) {
	defer func() { err = errors.Join(err, release(ports)) }()
	for _, p := range ports {
		if err := p.apply(point{Available: true, PDR: 1}, limit); err != nil {
			return fmt.Errorf("claim %s: %w", p.link.Attrs().Name, err)
		}
	}
	now := time.Now()
	start := now
	if strings.HasPrefix(startArg, "+") {
		d, err := time.ParseDuration(startArg[1:])
		if err != nil || d < 0 {
			return fmt.Errorf("invalid start offset %q", startArg)
		}
		start = now.Add(d)
	} else if startArg != "" {
		target, err := time.Parse(time.RFC3339Nano, startArg)
		if err != nil {
			return err
		}
		if !target.After(now) {
			return fmt.Errorf("scheduled start %s has already passed", target)
		}
		start = now.Add(target.Sub(now)) // retain monotonic component
	}
	// A long epoll timeout can be coalesced by Linux. Waiting on timerfd
	// gives each transition its own high-resolution kernel timer.
	fd, err := unix.TimerfdCreate(unix.CLOCK_MONOTONIC, unix.TFD_CLOEXEC|unix.TFD_NONBLOCK)
	if err != nil {
		return err
	}
	timer := os.NewFile(uintptr(fd), "satlink-replay-timer")
	defer timer.Close()
	cancelRead := context.AfterFunc(ctx, func() { timer.Close() })
	defer cancelRead()
	slog.Info("armed", "start", start, "sha256", hash, "ground", ports[0].link.Attrs().Name, "terminal", ports[1].link.Attrs().Name, "queue_packets", limit, "max_lateness_ns", tolerance.Nanoseconds())
	for i, s := range points {
		if remaining := time.Until(start.Add(s.At)); remaining > 0 {
			spec := unix.ItimerSpec{Value: unix.NsecToTimespec(remaining.Nanoseconds())}
			// Use SyscallConn.Control so cancellation cannot close and recycle
			// the descriptor during TimerfdSettime.
			raw, err := timer.SyscallConn()
			if err != nil {
				return err
			}
			var armErr error
			err = raw.Control(func(fd uintptr) {
				armErr = unix.TimerfdSettime(int(fd), 0, &spec, nil)
			})
			if ctx.Err() != nil {
				return nil
			}
			if err != nil || armErr != nil {
				return errors.Join(err, armErr)
			}
			var ticks [8]byte
			if _, err := timer.Read(ticks[:]); err != nil && ctx.Err() == nil {
				return err
			}
		}
		if ctx.Err() != nil {
			return nil
		}
		var outageEnd time.Time
		if s.down() && (i == 0 || !points[i-1].down()) {
			for _, later := range points[i+1:] {
				if !later.down() {
					outageEnd = start.Add(later.At)
					break
				}
			}
		}
		if err := transition(ports, s, start, outageEnd, limit, tolerance); err != nil {
			return err
		}
	}
	slog.Info("holding_final_state")
	<-ctx.Done()
	return nil
}

func run() error {
	trace := flag.String("trace", "", "timeline JSON file")
	terminal := flag.String("terminal", "", "terminal-facing Linux bridge port (downlink)")
	ground := flag.String("ground", "", "ground-facing Linux bridge port (uplink)")
	limit := flag.Uint64("queue-packets", 65536, "queue limit per direction")
	start := flag.String("start", "", "RFC3339 start time or +duration after arming (default now)")
	tolerance := flag.Duration("max-lateness", 0, "abort if an update is acknowledged later than this tolerance (0 records lateness only)")
	check := flag.Bool("check", false, "validate and print effective states without accessing interfaces")
	flag.Parse()
	if *tolerance < 0 {
		return fmt.Errorf("max-lateness must be nonnegative")
	}
	if *trace == "" || flag.NArg() != 0 || *limit == 0 || *limit > math.MaxUint32 {
		return fmt.Errorf("a trace and valid queue limit are required; use -help")
	}
	data, err := os.ReadFile(*trace)
	if err != nil {
		return err
	}
	points, err := parseTrace(data)
	if err != nil {
		return fmt.Errorf("%s: %w", *trace, err)
	}
	if err := validateDelays(points); err != nil {
		return err
	}
	if *check {
		return json.NewEncoder(os.Stdout).Encode(points)
	}
	ports, err := bridgePorts(*terminal, *ground)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return replay(ctx, ports, points, *start, uint32(*limit), fmt.Sprintf("%x", sha256.Sum256(data)), *tolerance)
}

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stderr, nil)))
	if err := run(); err != nil {
		slog.Error("satlink", "error", err)
		os.Exit(1)
	}
}

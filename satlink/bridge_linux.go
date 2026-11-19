package main

import (
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net"
	"time"

	"github.com/vishvananda/netlink"
)

const (
	upHandle   uint32 = 0x5a710000
	downHandle uint32 = 0x5a720000
)

type port struct {
	link  netlink.Link
	queue *netlink.Netem // assigned only after the kernel acknowledges installation
}

func bridgePorts(terminal, ground string) ([]*port, error) {
	if terminal == "" || ground == "" || terminal == ground {
		return nil, fmt.Errorf("specify two distinct bridge ports with -terminal and -ground")
	}
	var ports []*port
	for _, name := range []string{ground, terminal} {
		link, err := netlink.LinkByName(name)
		if err != nil {
			return nil, fmt.Errorf("%s: %w", name, err)
		}
		if link.Attrs().Flags&net.FlagUp == 0 || link.Attrs().MasterIndex == 0 {
			return nil, fmt.Errorf("%s must be an administratively up bridge port", name)
		}
		master, err := netlink.LinkByIndex(link.Attrs().MasterIndex)
		if err != nil {
			return nil, err
		}
		if master.Type() != "bridge" || master.Attrs().Flags&net.FlagUp == 0 {
			return nil, fmt.Errorf("%s must belong to an active Linux bridge", name)
		}
		if len(ports) > 0 && ports[0].link.Attrs().MasterIndex != link.Attrs().MasterIndex {
			return nil, fmt.Errorf("ports belong to different bridges")
		}
		qs, err := netlink.QdiscList(link)
		if err != nil {
			return nil, err
		}
		for _, q := range qs {
			// Even a default mq root can have user-configured child queues.
			if q.Attrs().Handle != 0 && q.Type() != "clsact" && q.Type() != "ingress" {
				return nil, fmt.Errorf("%s already has qdisc %s %s", name, q.Type(), netlink.HandleStr(q.Attrs().Handle))
			}
		}
		ports = append(ports, &port{link: link})
	}
	return ports, nil
}

func validateDelays(points []point) error {
	ticks := netlink.TickInUsec()
	if ticks <= 0 {
		return fmt.Errorf("cannot determine kernel traffic-control clock")
	}
	for _, p := range points {
		us := math.Round(p.DelayMS * 1000)
		if us > math.MaxUint32 || us*ticks > math.MaxUint32 {
			return fmt.Errorf("at %s: delay exceeds netem clock range", p.At)
		}
	}
	return nil
}

func (p *port) apply(s point, limit uint32) error {
	handle := upHandle
	latency := uint32(math.Round(s.DelayMS * 1000))
	loss := uint32(math.Round((1 - s.PDR) * math.MaxUint32))
	if s.down() {
		handle, latency, loss = downHandle, 0, math.MaxUint32
	}
	q := netlink.NewNetem(netlink.QdiscAttrs{LinkIndex: p.link.Attrs().Index, Handle: handle, Parent: netlink.HANDLE_ROOT}, netlink.NetemQdiscAttrs{Latency: latency, Limit: limit})
	q.Loss = loss
	var err error
	switch {
	case p.queue == nil:
		// Exclusive add also resolves concurrent attempts to claim the same port.
		err = netlink.QdiscAdd(q)
	case p.queue.Handle != q.Handle:
		err = netlink.QdiscReplace(q)
	case p.queue.Latency == q.Latency && p.queue.Loss == q.Loss:
		return nil
	default:
		err = netlink.QdiscChange(q)
	}
	if err == nil {
		p.queue = q
	}
	return err
}

var errQueueMissing = errors.New("controller qdisc missing")

func (p *port) snapshot() (*netlink.QdiscStatistics, error) {
	qs, err := netlink.QdiscList(p.link)
	if err != nil {
		return nil, err
	}
	for _, q := range qs {
		if q.Attrs().Parent == netlink.HANDLE_ROOT && q.Attrs().Handle == p.queue.Handle && q.Type() == "netem" {
			return q.Attrs().Statistics, nil
		}
	}
	return nil, fmt.Errorf("%s: %w", p.link.Attrs().Name, errQueueMissing)
}

func release(ports []*port) error {
	var errs []error
	for _, p := range ports {
		if p.queue == nil {
			continue
		}
		stats, err := p.snapshot()
		if err == nil {
			slog.Info("final_queue", "port", p.link.Attrs().Name, "stats", stats)
		}
		// A failed statistics dump must not leave a live impairment behind.
		// RTM_DELQDISC identifies the root by interface and expected handle.
		if !errors.Is(err, errQueueMissing) {
			err = errors.Join(err, netlink.QdiscDel(p.queue))
		}
		if err != nil {
			errs = append(errs, fmt.Errorf("release %s: %w", p.link.Attrs().Name, err))
		}
	}
	return errors.Join(errs...)
}

func transition(ports []*port, s point, start, outageEnd time.Time, limit uint32, tolerance time.Duration) error {
	type observation struct {
		port      *port
		before    *netlink.QdiscStatistics
		sent, ack time.Duration
	}
	// Keep statistics dumps and JSON output out of the window between the
	// two changes. The kernels on the two egress paths share one replay clock.
	updates := make([]observation, len(ports))
	for i, p := range ports {
		stats, err := p.snapshot()
		if err != nil {
			return err
		}
		updates[i] = observation{port: p, before: stats}
	}
	completed := 0
	defer func() {
		for _, u := range updates[:completed] {
			slog.Info("transition", "port", u.port.link.Attrs().Name, "state", s,
				"sent_ns", u.sent.Nanoseconds(), "ack_ns", u.ack.Nanoseconds(), "previous_stats", u.before)
		}
	}()
	for i := range updates {
		u := &updates[i]
		u.sent = time.Since(start)
		if !outageEnd.IsZero() && u.sent >= outageEnd.Sub(start) {
			return fmt.Errorf("%s missed outage at %s", u.port.link.Attrs().Name, s.At)
		}
		if err := u.port.apply(s, limit); err != nil {
			return fmt.Errorf("%s at %s: %w", u.port.link.Attrs().Name, s.At, err)
		}
		u.ack = time.Since(start)
		completed++
		if !outageEnd.IsZero() && u.ack >= outageEnd.Sub(start) {
			return fmt.Errorf("%s failed to confirm outage at %s before recovery", u.port.link.Attrs().Name, s.At)
		}
		if tolerance > 0 && u.ack-s.At > tolerance {
			return fmt.Errorf("%s at %s: lateness %s exceeds %s", u.port.link.Attrs().Name, s.At, u.ack-s.At, tolerance)
		}
	}
	return nil
}

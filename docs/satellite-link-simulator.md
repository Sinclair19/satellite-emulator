# Transparent satellite link simulator design

Use a separate Linux node with two data interfaces. A Linux bridge forwards
frames between them. A small Go controller replays an offline ns-3 timeline
and programs Linux traffic control through rtnetlink. The ground endpoints run
the real protocols. The simulator models the packet-level effects of the
service link (terminal to satellite), feeder link (satellite to gateway), and
switching between satellites.

## Placement

```text
freeRANUE secondary access
  UE ────── [terminal port | satellite bridge | ground port] ────── ground SN
  UE ───────────────────── terrestrial access ──────────────────── ground MN

N3IWF secondary access
  n3iwue ── [terminal port | satellite bridge | ground port] ────── ground N3IWF
            <────────────── outer NWu IP path ──────────────────>
                                                                  ├─ N2 → AMF
                                                                  └─ N3 → UPF
```

Use a dedicated pair of interfaces or VLANs for the satellite leg. Wire the
endpoints so traffic crosses the bridge in both directions. Keep management,
the terrestrial master leg, Xn, and core-facing N2/N3 on their existing
networks. Bridge the data ports at layer 2 to preserve endpoint IPs, ports,
and MAC addresses. Each bridge represents one link and applies one timeline
to all frames crossing it.

For freeRANUE, put the UE–secondary gNB (SN) access path on this segment and
set `gnb.ntn.enabled: false` on the SN to apply the trace exactly once. For
N3IWF, put the **outer** interface of `n3iwue` on the satellite path. Ordinary
IP payloads enter the tunnel at the terminal. The bridge carries IKE and both
native and UDP-encapsulated ESP, including setup, rekey, and data traffic.
IPsec keys and tunnel handling belong to the endpoints. If the gNB-side host
originates the payload, route it into the terminal's tunnel on that host.
[free5GC N3IWUE design](https://free5gc.org/doc/N3IWUE/n3iwue/)

## From ns-3 to a replay

1. Select one simulated terminal, the ground gateway, and a common time origin.
   Use the executed serving-satellite switches from the handover log. A
   candidate measurement or handover preparation event alone does not move
   traffic to the candidate satellite.
2. For each serving interval, join the selected satellite's timestamped link
   conditions. Export the **one-way** service plus feeder delay in milliseconds
   and the packet delivery ratio (PDR) for one traversal, in the range 0–1.
   When the run supplies only geometry, compute propagation delay from the
   sum of the two slant ranges divided by the speed of light. If measured PDR
   is unavailable, use an explicitly recorded experiment value, such as 1 for a
   propagation-only run. Preserve handover timestamps independently of any
   averaging window used for channel measurements.
3. If execution records bracket an interruption, emit its start and end. If
   the log gives only a switch instant, use that as the switch start and supply
   a named experiment parameter for the gap duration. A zero gap produces a
   direct satellite/profile update. Record the run, UE, time origin, and any
   assumed values alongside the trace. The upstream single-satellite converter
   supplies channel samples. Assembling a multi-satellite timeline is a
   separate export step. [ns-3 model outputs and tools](https://github.com/reki9185/ntn-channel-model#output-files)

Reuse freeRANUE's JSON array and `ntn.ParseTrace`. Time is seconds from
replay start. Missing fields inherit the preceding value. The parser accepts
`delay_ms` or the two legacy delay components and recognizes
`handover_start`/`handover_end` as link-down/up events. A change of satellite
label alone updates metadata. The ground endpoint and UE session identities
are independent of the satellite label. The reference parser is in `ntn/trace.go` in the freeRANUE checkout.

## Packet handling and timing

| Trace state | Action on each data port |
| --- | --- |
| Available | Use one root `netem` queue on egress. Ground-facing egress shapes uplink, terminal-facing egress shapes downlink. Each gets the full one-way delay and independent random loss of `100 × (1 − PDR)` percent. |
| Delay or PDR update while available | Change the live queue parameters in place. New arrivals use the new profile. Packets already scheduled keep their release times. |
| Link down or PDR zero | Graft a fresh 100%-loss queue with a different handle onto each egress. This discards the old queue's backlog and drops arrivals throughout the outage. Keep the bridge ports administratively up. |
| Link up with positive PDR | Install fresh queues with the recovered satellite's delay and PDR. Frames can enter immediately and experience the new propagation delay. |

Use distinct qdisc handles at outage boundaries so the kernel replaces the
queue instance. An in-place loss change only affects new arrivals. Frames
already handed to a NIC may still reach a receiver around a boundary. Record
when each port update was sent and acknowledged so the experiment can measure
uplink/downlink skew. [Linux qdisc implementation](https://codebrowser.dev/linux/linux/net/sched/sch_api.c.html) · [netem parameters](https://man7.org/linux/man-pages/man8/tc-netem.8.html)

## Controller operation

- **Inputs.** Terminal-facing port, ground-facing port, trace file, replay start,
  and per-direction queue limit in packets. Size each queue above the expected
  peak packets per second multiplied by the maximum one-way delay in seconds,
  allowing for bursts. Verify offload settings so the units seen by netem match
  the packet units used to measure PDR.
- **Start.** Validate the complete trace and both bridge ports before arming.
  Use dedicated ports with default qdiscs and one controller per port pair.
  Pass traffic with zero modeled impairment during setup so UDP endpoint
  discovery or IKE negotiation can complete. Translate the selected start
  time once into a local monotonic deadline. Apply each subsequent timestamp
  relative to that deadline. Replay requires no live ns-3 connection or
  network-delivered switch command.
- **Observe.** Log the trace hash, time origin, interface names, queue limits,
  requested and acknowledged transitions, satellite, delay, and PDR. Snapshot
  per-port qdisc counters and backlog around transitions. Use endpoint sequence
  numbers for delivered/lost packets since qdisc counters alone cannot
  distinguish all drop causes or confirm remote delivery.
- **Finish or fail.** Hold the final profile until stop. On stop, remove the
  controller's qdiscs and return both ports to baseline. A failed port update
  or a missed entire outage invalidates the run. Report the affected timestamp
  and direction, restore both ports, and exit with an error. A restart starts
  a new replay.

## Acceptance tests

Use the checked-in trace for the first run of each topology. Its added
one-way delay is 8.25 ms from 30 s until the switch. At 60 s the link goes
down for 80 ms. At 60.080 s it comes back with 10.5 ms one-way delay. At 90 s
the added delay returns to zero. The 80 ms is a fixture parameter.
[Reference timeline](../examples/transparent-sn.json)

| Test | Evidence for acceptance |
| --- | --- |
| Bridge and replay | Run numbered packets in both directions at 1 ms intervals under light load. Confirm traversal and baseline delivery first. On healthy plateaus, median RTT should rise by approximately 16.5 ms and then 21 ms over baseline. For the first lab run, target ±2 ms on those medians and completion of each port update within 5 ms of its deadline. Record measured errors. |
| Outage under backlog | Arrange for identifiable packets to be held in the old delay queue at cutover. Verify that these and packets offered during the applied outage are absent at the far endpoint. New packets resume after recovery plus propagation delay. Report application interruption separately from the configured outage. |
| Partial loss | Replay a fixed profile with PDR 0.9. Send 10,000 numbered packets each way at light load. A first-run target is 89–91% delivered in each direction. |
| freeRANUE | Establish NR-DC and drive one flow selected for the SN and another for the MN. Observe the gap on the SN flow, continuous MN delivery, and SN recovery with the same UE address and tunnel IDs. Check gNB logs for any unexpected session or path switch. |
| N3IWF | Establish the tunnel and drive a flow through it to the data network. Capture the outer packets on the terminal/gateway and count the inner packets at the endpoints. For the 80 ms fixture, schedule outside a planned rekey and verify recovery on the same IPsec security associations and PDU session. For longer outages, record actual retransmission or tunnel recovery behavior. |

The implementation deliverables are the ns-3 trace exporter and the
standalone Go replay controller. The JSON parser, example trace, and
in-process SN adapter already exist in the companion freeRANUE checkout. The two topology
tests above exercise the same controller binary and trace.

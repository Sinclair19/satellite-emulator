# Run the transparent satellite link

`satlink` runs on the Linux machine between the terminal and the ground endpoint.
It programs a netem egress queue on each data interface. The kernel forwards
the packets. The same binary handles freeRANUE access UDP, ordinary IP, and
N3IWF traffic including IKE, ESP and UDP-encapsulated ESP.

## Build and check a timeline

Run these commands from the repository root on Linux with Go 1.23 or newer.
The controller has its own Go module. Its build uses the dependencies in
`satlink/go.mod` independently of the upstream emulator's free5GC checkout.

```sh
CGO_ENABLED=0 go -C satlink build -trimpath -o ../bin/satlink .
bin/satlink -check -trace examples/transparent-sn.json
```

`-check` prints the effective state at each transition, including inherited
values and timestamps in nanoseconds. It works as an ordinary user. A replay
needs Linux bridge and `sch_netem` support and `CAP_NET_ADMIN` in the network
namespace that owns the ports.

## Wire the middle node

Provide two dedicated Ethernet interfaces or VLAN interfaces on the same Linux
bridge. Both endpoint hosts use their existing IP addresses on that segment.
For example, on an unconfigured lab node whose data ports are named
`sat-term` and `sat-ground`, create the bridge once:

```sh
sudo ip link add br-sat type bridge
sudo ip link set sat-term master br-sat
sudo ip link set sat-ground master br-sat
sudo ip link set sat-term up
sudo ip link set sat-ground up
sudo ip link set br-sat up
bridge link show master br-sat
```

- **freeRANUE.** Connect the UE-facing UDP path of the ground secondary gNB to
  the satellite segment. Disable its in-process impairment with
  `gnb.ntn.enabled: false`. Keep the terrestrial master leg and core interfaces
  on their existing segments. Verify a flow selected for the secondary leg
  crosses both bridge ports.
- **N3IWF.** Connect the terminal's outer NWu interface and the ground N3IWF's
  outer interface through the bridge. Preserve the IPsec endpoints' existing
  addressing and route the test flow through their tunnel. Capture outer
  packets on the data segment and measure the application flow inside the
  tunnel. N2 and N3 run from the N3IWF to the core on the ground side.

Use the management network to start the controller and collect its logs. For
packet-level loss measurements, check the data interfaces' segmentation,
aggregation, and hardware forwarding settings so frames traverse the software
queues at the intended packet granularity.

## Replay and collect results

```sh
sudo bin/satlink -terminal sat-term -ground sat-ground \
  -trace examples/transparent-sn.json -start +15s \
  -queue-packets 65536 2>satlink-run.jsonl
```

During the 15-second lead-in both ports pass traffic. An RFC3339 timestamp
with a timezone can also specify a common start. Omitting `-start` begins
replay as soon as the queues are installed. The last state persists until
SIGINT or SIGTERM. A clean stop restores the default queues on both ports.
A port with a configured egress queue is rejected before replay.

The example adds 8.25 ms **per direction** at 30 s, discards queued and incoming
packets at 60 s, and recovers at 60.080 s with 10.5 ms per direction. At 90 s
the added delay returns to zero. Look for a healthy-plateau **RTT increase**
of about 16.5 ms and then 21 ms. Use numbered packets for the gap measurement.
Each direction has its own kernel random loss stream. Replaying a trace
reproduces the schedule, while individual random packet losses can vary.

The controller writes JSON lines to stderr. The `armed` record includes the
trace SHA-256 and replay origin. Each `transition` records the egress port,
state, previous queue statistics, and send/ack times relative to the origin.
Subtract `state.time_ns` from `ack_ns` to measure update lateness. Set
`-max-lateness 5ms` to invalidate a run and restore the queues if either port
exceeds a 5 ms deadline tolerance. The default records the observed times. Compare
both ports to measure skew. Queue statistics reset when a fresh queue replaces
the old one. An unconfirmed or entirely missed outage makes the run fail.
For mid-run inspection use `tc -s qdisc show dev sat-ground` and the equivalent
command for `sat-term`.

After a forced kill the kernel can retain the qdiscs. With the controller
stopped, inspect both ports with `tc qdisc show`. If the root is the simulator's
`netem 5a71:` (available) or `netem 5a72:` (outage), remove that specific
handle on that port, for example `sudo tc qdisc del dev sat-term root handle
5a71:`. Repeat for the other port using its observed handle.

## Export an ns-3 run

Run the exporter with Python 3.11 or newer against the completed strategy's
result directory. Use one ns-3 UE ID per replay. This example selects UE 0
and assumes a 600 km satellite-to-ground gateway slant range.

```sh
python3 tools/export_ns3.py /path/to/ns3/result/tdcho --ue 0 \
  --feeder-km 600 --output ue0.json
bin/satlink -check -trace ue0.json
```

- **Delay.** The exporter sums the logged UE slant range and the supplied
  feeder range, and divides by the speed of light. For a moving gateway link,
  replace `--feeder-km` with `--feeder-csv ranges.csv`. Use columns
  `time_s,satellite,feeder_km` on the same simulation clock. Supply a sample
  for each serving satellite by the time it first serves the UE. A range
  remains in effect until its next sample.
- **Delivery.** By default it reads `sgp4-multi-snr-log.csv` and converts
  logged PER to PDR. At a successful switch it uses the new satellite's
  logged distance and SNR immediately, applying the model's packet-error
  formula until the next PER sample. Set `--packet-bytes` to match the run
  (upstream default 1250). This treats the feeder as lossless. For an assumed
  end-to-end PDR, such as `--pdr 1` for propagation-only measurements, it
  reads `sgp4-multi-channel-log.csv` instead.
- **Switch timing.** It pairs `EXECUTION_TRIGGERED` rows in
  `cho-detailed-log.csv` with outcomes in `sgp4-handover-log.csv`. A failed
  execution recovers on the source satellite. An execution still open at the
  end of the run leaves the link down. If a success has only a completion
  timestamp, provide `--gap-ms` explicitly. That assumed outage **ends** at
  the upstream completion time. Use zero for a switch without a gap.
- **Provenance.** The first event includes the input filenames, SHA-256 hashes,
  UE ID, time origin, and modeling assumptions. Export replaces the output
  atomically after validation. Apply a separate trace to each independently
  modeled UE/port pair.

### Reproduce the included ns-3 handover

The included UE 0 trace comes from a 15-second TDCHO simulation using the
upstream Washington DC dataset and default radio parameters. The logged switch
from STARLINK-35574 to STARLINK-30481 starts at 9.00000 s and completes at
9.04571 s. The 600 km feeder range is the explicit assumption used for this
example. [Replay trace](../examples/ns3-tdcho-ue0.json) · [ns-3 source revision](https://github.com/reki9185/ntn-channel-model/tree/1c67dffc0b56ad7958763bf3a187123fb3385de2)

From the ns-3 checkout at that revision, build the TDCHO target with CMake
and Ninja. The commands below invoke the compiled scenario directly. Set
`simulator_dir` to the path of this simulator repository.

```sh
cmake -S . -B cmake-cache -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DNS3_WARNINGS_AS_ERRORS=OFF -DNS3_TESTS=OFF -DNS3_EXAMPLES=OFF
cmake --build cmake-cache --target scratch_MultiUE-CHO-SNR-TDCHO_SNR-CHO-Multi-TDCHO
mkdir -p result
build/scratch/MultiUE-CHO-SNR-TDCHO/ns3-dev-SNR-CHO-Multi-TDCHO --simTime=15
python3 "$simulator_dir/tools/export_ns3.py" result/tdcho --ue 0 \
  --feeder-km 600 --output ue0.json
```

## Verify on Linux

```sh
go -C satlink test -race ./...
python3 -B -m unittest discover -s tests
unshare -Urn python3 tests/bridge_smoke.py bin/satlink
unshare -Urn python3 tests/bridge_smoke.py bin/satlink --esp --reference
```

The smoke test creates an isolated bridge and two endpoints. It measures
both directions, checks for packets leaking from the pre-switch queue, sends
10,000 probes per direction through a 90% PDR profile, and verifies clean
shutdown. The default run shortens the healthy plateaus. `--reference`
preserves the full 90-second reference schedule. `--esp` applies native ESP
with Linux XFRM security associations between the test endpoints. These tests
require iproute2, ping, util-linux and permission to create user/network
namespaces. Full UE registration, IKE negotiation, and PDU-session continuity
are separate integration tests with the deployed core.

### Reference measurements

On a WSL2 Linux bridge with two veth ports, the following runs used a 5 ms
deadline tolerance. Each PDR figure counts 10,000 packets per direction with
a configured delivery probability of 0.90. The gap is the time between the
kernel acknowledgments for installing the outage and recovery queues.

| Traffic and schedule | Applied gap, UL / DL | Observed PDR, UL / DL | Largest update lateness |
| --- | --- | --- | --- |
| UDP, shortened plateaus | 79.830 / 79.805 ms | 0.8981 / 0.9060 | 0.582 ms |
| Native ESP, full reference schedule | 80.047 / 79.990 ms | 0.8969 / 0.9021 | 0.559 ms |

In the ESP run the median one-way delays were 8.36 / 8.36 ms before the
switch and 10.68 / 10.66 ms after recovery. Each direction discarded four
identified pre-switch packets still queued at cutover. The separate 15-second
ns-3 example produced a 45.710 ms modeled outage and applied gaps of 45.806
and 45.792 ms in a bridge replay. Host scheduling and offered load affect
these measurements.

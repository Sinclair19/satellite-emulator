"""Run in a private user/network namespace. See docs/running-satlink.md."""

import argparse
import contextlib
import datetime
import json
import os
import select
import signal
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRAME = struct.Struct("!IQ")
IPS = ["192.0.2.1", "192.0.2.2"]


def run(*cmd):
    return subprocess.check_output(cmd, text=True)


def probe(ip, peer, start_ns, duration, output):
    received, sent = {}, []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((ip, 4500))
        sock.setblocking(False)
        next_send = start_ns
        end = start_ns + int(duration * 1e9)
        while time.monotonic_ns() < end + 100_000_000:
            now = time.monotonic_ns()
            if next_send <= now < end:
                seq = len(sent)
                sock.sendto(FRAME.pack(seq, now), (peer, 4500))
                sent.append((now - start_ns) / 1e6)
                next_send = max(next_send + 1_000_000, now)
            wait = (next_send - time.monotonic_ns()) / 1e9 if now < end else 0.005
            if select.select([sock], [], [], max(0, wait))[0]:
                data, _ = sock.recvfrom(1024)
                seq, t = FRAME.unpack(data)
                received[seq] = (time.monotonic_ns() - t) / 1e6
    Path(output).write_text(json.dumps([sent, received]))


def configure_esp(peers):
    key = "0x" + os.urandom(20).hex()
    for i, ns in enumerate(peers):
        for j, src in enumerate(IPS):
            dst = IPS[1 - j]
            spi = str(100 + j)
            run(
                *ns,
                "ip",
                "xfrm",
                "state",
                "add",
                "src",
                src,
                "dst",
                dst,
                "proto",
                "esp",
                "spi",
                spi,
                "mode",
                "transport",
                "aead",
                "rfc4106(gcm(aes))",
                key,
                "128",
            )
            run(
                *ns,
                "ip",
                "xfrm",
                "policy",
                "add",
                "src",
                src,
                "dst",
                dst,
                "dir",
                "out" if i == j else "in",
                "tmpl",
                "src",
                src,
                "dst",
                dst,
                "proto",
                "esp",
                "mode",
                "transport",
                "level",
                "required",
            )


def measure(directory, peers, binary, full, tolerance):
    trace = json.loads((ROOT / "examples/transparent-sn.json").read_text())
    if not full:
        for state, t in zip(trace, [0, 0.5, 1.4, 1.48, 2.4]):
            state["time"] = t
    down, up, reset = [e["time"] for e in trace[2:]]
    loss_time = reset + 0.4
    trace.append(dict(time=loss_time, pdr=0.9))
    profile = directory / "trace.json"
    profile.write_text(json.dumps(trace))
    duration = loss_time + 11
    start_wall = time.time() + 1
    start_ns = time.monotonic_ns() + round((start_wall - time.time()) * 1e9)
    stamp = datetime.datetime.fromtimestamp(
        start_wall, datetime.timezone.utc
    ).isoformat()
    args = [
        binary,
        "-ground",
        "ground",
        "-terminal",
        "terminal",
        "-trace",
        profile,
        "-max-lateness",
        tolerance,
    ]
    log = directory / "controller.log"
    with log.open("w") as f, contextlib.ExitStack() as jobs:
        ctl = subprocess.Popen([*args, "-start", stamp], stderr=f)
        jobs.callback(stop, ctl)
        procs = []
        for i, ns in enumerate(peers):
            p = subprocess.Popen(
                [
                    *ns,
                    sys.executable,
                    __file__,
                    "probe",
                    IPS[i],
                    IPS[1 - i],
                    str(start_ns),
                    str(duration),
                    directory / str(i),
                ]
            )
            jobs.callback(stop, p)
            procs.append(p)
        time.sleep(0.2)
        assert ctl.poll() is None, log.read_text()
        duplicate = subprocess.run(args, capture_output=True, timeout=3)
        assert duplicate.returncode != 0 and b"already has qdisc" in duplicate.stderr, (
            duplicate
        )
        deadline = time.monotonic() + duration + 4
        while any(p.poll() is None for p in procs):
            assert ctl.poll() is None, log.read_text()
            assert time.monotonic() < deadline, "probe timeout"
            time.sleep(0.1)
        assert all(p.returncode == 0 for p in procs)
        ctl.send_signal(signal.SIGTERM)
        assert ctl.wait(timeout=3) == 0, log.read_text()
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    transitions = [e for e in entries if e["msg"] == "transition"]
    assert len(transitions) == len(trace) * 2
    skew_ms = max((e["ack_ns"] - e["state"]["time_ns"]) / 1e6 for e in transitions)
    recordings = [json.loads((directory / str(i)).read_text()) for i in range(2)]
    for i, port in enumerate(["ground", "terminal"]):
        sent = recordings[i][0]
        received = recordings[1 - i][1]

        def interval(start, end):
            return [j for j, ms in enumerate(sent) if start <= ms < end]

        medians = []
        for begin, end, delay in [
            (0.12, trace[1]["time"], 0),
            (trace[1]["time"], down, 8.25),
            (up, reset, 10.5),
        ]:
            ids = interval(1000 * begin + 30, 1000 * end - 30)
            values = [received[str(j)] for j in ids if str(j) in received]
            assert len(values) >= 0.99 * len(ids) and ids, (port, len(values), len(ids))
            med = statistics.median(values)
            assert abs(med - delay) < 1.5, (port, med, delay)
            medians.append(round(med, 2))
        cut = next(
            e
            for e in transitions
            if e["port"] == port and not e["state"]["link_available"]
        )
        recovery = next(
            e
            for e in transitions
            if e["port"] == port and e["state"]["time_ns"] == round(up * 1e9)
        )
        actual_gap = (recovery["ack_ns"] - cut["ack_ns"]) / 1e6
        # Frames sent 2–6 ms before cutover were still in the 8.25 ms queue.
        queued = interval(cut["sent_ns"] / 1e6 - 6, cut["sent_ns"] / 1e6 - 2)
        outage = interval(cut["ack_ns"] / 1e6 + 2, 1000 * up - 2)
        assert (
            queued and outage and all(str(j) not in received for j in queued + outage)
        )
        ids = interval(1000 * loss_time + 50, 1000 * duration)[:10000]
        assert len(ids) == 10000
        pdr = sum(str(j) in received for j in ids) / len(ids)
        # Statistical test: use a wide failure threshold and print the observed value.
        assert 0.88 < pdr < 0.92, pdr
        print(
            f"{port}: median one-way ms {medians}; 10,000-packet PDR {pdr:.4f}; "
            f"{len(queued)} queued and {len(outage)} outage probes dropped; applied gap {actual_gap:.3f} ms"
        )
        assert "netem" not in run("tc", "qdisc", "show", "dev", port)
    print(f"Maximum deadline-to-ack latency {skew_ms:.3f} ms")


def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument(
        "--max-lateness", default="0", help="controller timing tolerance, e.g. 5ms"
    )
    parser.add_argument("--esp", action="store_true")
    parser.add_argument(
        "--reference", action="store_true", help="use full 90-second fixture timing"
    )
    args = parser.parse_args()
    if len(json.loads(run("ip", "-j", "link"))) != 1:
        parser.error("run inside a fresh network namespace")
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as jobs:
        run("ip", "link", "add", "br0", "type", "bridge")
        run("ip", "link", "set", "br0", "up")
        peers = []
        for i, port in enumerate(["terminal", "ground"]):
            proc = subprocess.Popen(["unshare", "-n", "sleep", "infinity"])
            jobs.callback(stop, proc)
            for _ in range(100):
                if os.readlink(f"/proc/{proc.pid}/ns/net") != os.readlink(
                    "/proc/self/ns/net"
                ):
                    break
                time.sleep(0.01)
            else:
                raise RuntimeError("endpoint namespace did not start")
            ns = ["nsenter", "-t", str(proc.pid), "-n"]
            peers.append(ns)
            run("ip", "link", "add", port, "type", "veth", "peer", "name", "edge")
            run("ip", "link", "set", "edge", "netns", str(proc.pid))
            run("ip", "link", "set", port, "master", "br0")
            run("ip", "link", "set", port, "up")
            run(*ns, "ip", "addr", "add", IPS[i] + "/24", "dev", "edge")
            run(*ns, "ip", "link", "set", "edge", "up")
        if args.esp:
            configure_esp(peers)
        run(*peers[0], "ping", "-c", "1", "-W", "1", IPS[1])
        measure(
            Path(tmp), peers, args.binary.resolve(), args.reference, args.max_lateness
        )


if __name__ == "__main__":
    if sys.argv[1:2] == ["probe"]:
        probe(
            sys.argv[2], sys.argv[3], int(sys.argv[4]), float(sys.argv[5]), sys.argv[6]
        )
    else:
        main()

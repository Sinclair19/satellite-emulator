#!/usr/bin/env python3
"""Export one transparent-link timeline from an ntn-channel-model MultiUE run."""

import argparse
import bisect
import csv
import hashlib
import json
import math
import tempfile
from collections import defaultdict
from pathlib import Path

LIGHT_KM_PER_MS = 299.792458


def number(value, name, minimum=0):
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"invalid {name}: {value!r}")
    return result


def rows(path, required, ue=None):
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        missing = set(required.split()) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        for row in reader:
            if ue is None or row["ue_id"] == ue:
                yield number(row["time_s"], "time_s"), row


def feeder_lookup(path, fixed):
    if path is None:
        km = number(fixed, "feeder-km")
        return lambda t, sat: km, []
    ranges = defaultdict(list)
    changes = []
    for t, row in rows(path, "time_s satellite feeder_km"):
        sat = row["satellite"]
        ranges[sat].append((t, number(row["feeder_km"], "feeder_km")))
        changes.append((t, "feeder", sat))
    for samples in ranges.values():
        samples.sort()
        if any(a[0] == b[0] for a, b in zip(samples, samples[1:])):
            raise ValueError("duplicate feeder sample for one satellite and timestamp")

    def lookup(t, sat):
        samples = ranges[sat]
        i = bisect.bisect_right(samples, (t, math.inf))
        if i == 0:
            raise ValueError(f"missing feeder range for {sat} at {t}s")
        return samples[i - 1][1]

    return lookup, changes


def load_handovers(path, detail, ue, gap_ms):
    # Per-attempt source/target matching also handles repeated visits to a satellite.
    records = []
    if detail.exists():
        for t, row in rows(detail, "time_s ue_id event source_cell target_cell", ue):
            if row["event"] == "EXECUTION_TRIGGERED":
                records.append(
                    (t, "start", row["source_cell"], row["target_cell"], row)
                )
    for t, row in rows(
        path, "time_s ue_id old_satellite new_satellite doppler_jump_kHz", ue
    ):
        failed = (row["doppler_jump_kHz"] or "").startswith("FAILURE_")
        records.append(
            (
                t,
                "failure" if failed else "complete",
                row["old_satellite"],
                row["new_satellite"],
                row,
            )
        )
    records.sort(key=lambda r: (r[0], r[1] != "start"))
    pending = None
    events = []
    for t, kind, old, new, row in records:
        if kind == "start":
            if pending is not None:
                raise ValueError(f"overlapping execution at {t}s")
            pending = (t, old, new)
            continue
        if pending is not None:
            start, src, dst = pending
            if (src, dst) != (old, new):
                raise ValueError(f"unmatched handover at {t}s: {old} to {new}")
            pending = None
        elif kind == "failure":
            continue  # failure before execution did not interrupt the serving link
        elif gap_ms is None:
            raise ValueError(
                f"missing execution start for {old} to {new} at {t}s; supply --gap-ms"
            )
        else:
            start = t - gap_ms / 1000
        if start < 0:
            raise ValueError(f"assumed outage starts before the run at {t}s")
        if start < t:
            events.append((start, "down", old))
        events.append((t, kind, row))
    if pending is not None:
        events.append((pending[0], "down", pending[1]))
    return events


def export(args):
    run = args.run
    pdr = args.pdr
    if pdr is not None and not 0 <= pdr <= 1:
        raise ValueError("pdr must be in [0,1]")
    if args.gap_ms is not None:
        number(args.gap_ms, "gap-ms")
    if args.packet_bytes <= 0:
        raise ValueError("packet-bytes must be positive")
    sample_path = run / (
        "sgp4-multi-channel-log.csv" if pdr is not None else "sgp4-multi-snr-log.csv"
    )
    handover_path = run / "sgp4-handover-log.csv"
    detail_path = run / "cho-detailed-log.csv"
    paths = [sample_path, handover_path]
    if detail_path.exists():
        paths.append(detail_path)
    if args.feeder_csv is not None:
        paths.append(args.feeder_csv)
    if args.output.resolve() in [p.resolve() for p in paths]:
        raise ValueError("output would overwrite a source log")
    feeder, events = feeder_lookup(args.feeder_csv, args.feeder_km)
    events += load_handovers(handover_path, detail_path, args.ue, args.gap_ms)
    fields = "time_s ue_id active_satellite distance_km"
    if pdr is None:
        fields += " per_percent ho_interrupting"
    count = 0
    for t, row in rows(sample_path, fields, args.ue):
        events.append((t, "sample", row))
        count += 1
    if not count:
        raise ValueError(f"no channel measurements for UE {args.ue}")
    # Complete before sampling the target at a shared timestamp. A down edge
    # follows the channel measurement at its start and takes effect immediately.
    order = {"complete": 0, "failure": 0, "sample": 1, "feeder": 2, "down": 3}
    events.sort(key=lambda e: (e[0], order[e[1]]))
    trace = []
    sat, distance, delivery = "", 0, 1
    interrupted = False
    switched_at = None
    for t, kind, data in events:
        if kind == "down":
            if sat != data or interrupted:
                raise ValueError(
                    f"inconsistent outage for {data} at {t}s, serving {sat}"
                )
            interrupted = True
        elif kind in ("complete", "failure"):
            if sat != data["old_satellite"]:
                raise ValueError(
                    f"unexpected source at {t}s: {data['old_satellite']} (serving {sat})"
                )
            interrupted = False
            if kind == "complete":
                sat = data["new_satellite"]
                switched_at = t
                distance = number(data.get("new_dist_km"), "new_dist_km")
                if pdr is None:
                    # Use the ns-3 BPSK BER/PER formula at completion until
                    # the first logged PER measurement of the new satellite.
                    snr = number(data.get("new_snr_dB"), "new_snr_dB", -math.inf)
                    ber = 0.5 * math.erfc(10 ** (min(snr, 100) / 20))
                    delivery = math.exp(8 * args.packet_bytes * math.log1p(-ber))
                else:
                    delivery = pdr
        elif kind == "sample":
            active = data["active_satellite"]
            if not active:
                raise ValueError(f"empty serving satellite at {t}s")
            if sat and active != sat:
                if switched_at == t:
                    continue  # old serving sample at the same rounded instant
                raise ValueError(
                    f"serving satellite changed to {active} without completion at {t}s"
                )
            sat = active
            distance = number(data["distance_km"], "distance_km")
            if pdr is not None:
                delivery = pdr
            elif data["ho_interrupting"] == "0":
                per = number(data["per_percent"], "per_percent")
                if per > 100:
                    raise ValueError(f"invalid PER at {t}s")
                delivery = 1 - per / 100
            elif data["ho_interrupting"] != "1":
                raise ValueError(f"invalid ho_interrupting at {t}s")
        elif data != sat:
            continue

        state = dict(
            satellite=sat,
            delay_ms=(distance + feeder(t, sat)) / LIGHT_KM_PER_MS,
            pdr=delivery,
            link_available=not interrupted,
        )
        if trace and trace[-1]["time"] == t:
            trace.pop()
        trace.append(dict(time=t, **state))
    if trace[0]["time"] > 0:
        trace.insert(0, dict(time=0, link_available=False))
    # Keep provenance inside the atomic output. Both replay parsers ignore this field.
    sources = []
    for path in paths:
        with path.open("rb") as f:
            sources.append(
                dict(
                    file=path.name, sha256=hashlib.file_digest(f, "sha256").hexdigest()
                )
            )
    trace[0]["metadata"] = dict(
        run=str(run),
        ue=args.ue,
        origin_s=0,
        feeder_km=args.feeder_km,
        pdr_override=pdr,
        fallback_gap_ms=args.gap_ms,
        packet_bytes=args.packet_bytes,
        sources=sources,
    )
    return trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="strategy result directory")
    parser.add_argument("--ue", required=True, help="ns-3 ue_id")
    parser.add_argument("--output", required=True, type=Path)
    feeder = parser.add_mutually_exclusive_group(required=True)
    feeder.add_argument(
        "--feeder-km", type=float, help="assumed satellite-to-gateway slant range"
    )
    feeder.add_argument(
        "--feeder-csv",
        type=Path,
        help="time_s,satellite,feeder_km for the chosen gateway",
    )
    parser.add_argument(
        "--pdr", type=float, help="override the logged PER with a fixed delivery ratio"
    )
    parser.add_argument(
        "--gap-ms",
        type=float,
        help="fallback interruption ending at a logged completion",
    )
    parser.add_argument(
        "--packet-bytes",
        type=int,
        default=1250,
        help="ns-3 packet size for estimating PER at a switch",
    )
    args = parser.parse_args()
    temp = None
    try:
        trace = export(args)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=args.output.parent, delete=False
        ) as f:
            temp = Path(f.name)
            json.dump(trace, f, indent=2, allow_nan=False)
            f.write("\n")
        temp.replace(args.output)
    except (OSError, ValueError, csv.Error) as exc:
        parser.exit(1, f"export_ns3: {exc}\n")
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

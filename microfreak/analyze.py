#!/usr/bin/env python3
"""Show the structure of a recorded SysEx capture.

    mfreak capture analyze captures/FILE.jsonl
    mfreak capture analyze captures/FILE.jsonl --presets

Groups messages by header and length, and shows which byte positions stay
constant and which vary. This is for working out an unknown protocol. For
speech audio, see `speech analyze`.
"""
import argparse
import collections
import json


def load(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("type") == "sysex":
                out.append((r.get("dir", r.get("src", "?")),
                            [int(b, 16) for b in r["raw"].split()]))
    return out


def dump_presets(msgs):
    """Decodes preset-name records: F0 00 20 6B 07 01 <seq> 23 .. slot .. name."""
    records = [m for _, m in msgs
               if len(m) == 45 and m[:6] == [0xF0, 0x00, 0x20, 0x6B, 0x07, 0x01] and m[7] == 0x23]
    if not records:
        print("No preset-name records (command 0x23) in this capture.")
        return

    def name_of(m):
        s = ""
        for x in m[21:-1]:
            if x == 0:
                break
            s += chr(x) if 0x20 <= x < 0x7F else "?"
        return s

    rows = sorted(((m[9] * 128 + m[10]), m) for m in records)
    slots = [i for i, _ in rows]
    print(f"{len(rows)} preset records, slots {min(slots)}..{max(slots)}")
    if len(set(slots)) != len(slots):
        print("  WARNING: duplicate slot numbers present")
    print()
    for i, m in rows:
        print(f"  {i:3}  b19={m[19]:2}  {name_of(m)}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", help="a .jsonl capture file")
    ap.add_argument("--header-len", type=int, default=6, metavar="N",
                    help="bytes that count as the header (default: 6)")
    ap.add_argument("--max-groups", type=int, default=8, metavar="N",
                    help="how many message groups to show (default: 8)")
    ap.add_argument("--presets", action="store_true",
                    help="decode preset-name records instead")
    args = ap.parse_args()

    msgs = load(args.capture)
    if not msgs:
        print("No SysEx messages found.")
        return

    if args.presets:
        dump_presets(msgs)
        return

    print(f"{len(msgs)} SysEx messages\n")

    lengths = collections.Counter(len(m) for _, m in msgs)
    print("Lengths:")
    for ln, n in lengths.most_common(10):
        print(f"  {ln:5} bytes  x{n}")

    groups = collections.defaultdict(list)
    for direction, m in msgs:
        key = (direction, " ".join(f"{b:02X}" for b in m[:args.header_len]), len(m))
        groups[key].append(m)

    print(f"\n{len(groups)} distinct (direction, header, length) groups")
    print("Showing the largest:\n")

    for (direction, header, length), members in sorted(
            groups.items(), key=lambda kv: -len(kv[1]))[:args.max_groups]:
        print("=" * 72)
        print(f"{direction}  header {header}  length {length}  count {len(members)}")

        # Which positions vary, and over what values?
        varying = []
        for pos in range(length):
            values = {m[pos] for m in members}
            if len(values) > 1:
                varying.append((pos, values))

        const = [p for p in range(length) if p not in {v[0] for v in varying}]
        print(f"  constant at {len(const)} positions, varying at {len(varying)}")

        if varying:
            print("  varying positions:")
            for pos, values in varying[:24]:
                vs = sorted(values)
                shown = " ".join(f"{v:02X}" for v in vs[:16])
                more = f" ... ({len(vs)} distinct)" if len(vs) > 16 else f" ({len(vs)} distinct)"
                step = ""
                if len(vs) > 2:
                    diffs = {b - a for a, b in zip(vs, vs[1:])}
                    if len(diffs) == 1:
                        step = f"  [uniform step {diffs.pop()}]"
                print(f"    byte {pos:3}: {shown}{more}{step}")
            if len(varying) > 24:
                print(f"    ... and {len(varying) - 24} more varying positions")

        print("  example:")
        ex = members[0]
        for off in range(0, min(len(ex), 64), 16):
            chunk = ex[off:off + 16]
            h = " ".join(f"{b:02X}" for b in chunk).ljust(47)
            a = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
            print(f"    {off:04d}  {h}  |{a}|")
        if len(ex) > 64:
            print(f"    ... {len(ex) - 64} more bytes")
        print()


if __name__ == "__main__":
    main()

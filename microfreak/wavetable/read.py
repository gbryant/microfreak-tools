#!/usr/bin/env python3
"""Read one wavetable's name and four frames off the instrument.

    mfreak wavetable read 3
    mfreak wavetable read 3 --out wt3.mfwz

--out saves it as an MCC wavetable file, which restores with
`wavetable write N --file wt3.mfwz`. An empty wavetable is reported as
empty, not as an error.
"""
import argparse
import sys

from microfreak.midi import open_synth, build, checksum_free_seq
from microfreak import panel

NAME_START = 14   # offset within the 32-byte first-chunk payload
STRAY_NULL_OFFSETS = {2, 10}  # relative to NAME_START; see microfreak-emu/docs/PROTOCOL.md
FLAGS = 4         # name-chunk byte MCC sets to EMPTY_FLAG when it empties a slot
EMPTY_FLAG = 0x08


def is_empty(name_block):
    """True for a slot with no name block, or one flagged empty."""
    return not name_block or bool(name_block[FLAGS] & EMPTY_FLAG)


def wavetable_name_of(first_chunk_payload):
    chars = []
    for i, b in enumerate(first_chunk_payload[NAME_START:]):
        if i in STRAY_NULL_OFFSETS:
            continue
        if b == 0:
            break
        chars.append(chr(b) if 0x20 <= b < 0x7F else "?")
    return "".join(chars)


def pull_block(synth, sub, hi, lo, seqgen, verbose=False, label=""):
    """Selects and drains one block. Returns the concatenated payload, or
    None if the slot is empty (select acked, but no data ever arrives).
    """
    seq = next(seqgen)
    synth.send(build(seq, 0x03, sub, hi, lo, 0x01))
    synth.recv(lambda m: len(m) == 10 and m[7] == 0x00 and m[8] == 0x15 and m[6] == seq)

    payload = bytearray()
    chunk_count = 0
    while True:
        seq = next(seqgen)
        synth.send(build(seq, 0x01, 0x18, 0x00))
        try:
            chunk = synth.recv(lambda m: len(m) == 42 and m[7] == 0x20 and m[6] == seq)
        except TimeoutError:
            if chunk_count == 0:
                if verbose:
                    print(f"  {label}: empty (no data)", file=sys.stderr)
                return None
            raise
        payload.extend(chunk[9:41])
        chunk_count += 1
        if chunk[8] == 0x17:
            break
        if chunk_count > 1000:
            raise RuntimeError("block did not terminate after 1000 chunks — aborting")

    if verbose:
        print(f"  {label}: {chunk_count} chunks, {len(payload)} bytes", file=sys.stderr)
    return bytes(payload)


def read_wavetable(synth, slot, seqgen, verbose=False):
    name_block = pull_block(synth, 0x57, slot, 0, seqgen, verbose, "name")
    name = None if is_empty(name_block) else wavetable_name_of(name_block)

    frames = []
    for frame in range(4):
        data = pull_block(synth, 0x55, slot, frame, seqgen, verbose, f"frame {frame}")
        frames.append(data)

    return name, frames


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("wavetable", type=int, metavar="WAVETABLE",
                    help="which wavetable (1-16)")
    ap.add_argument("--port", default="MicroFreak", help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--out", metavar="FILE.mfwz", help="save it as an MCC wavetable file")
    ap.add_argument("--timeout", type=float, default=6.0, help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    # Panel number in, 0-based slot out, straight after parsing so
    # everything below still sees the index it always did.
    args.slot = panel.to_slot(ap, args.wavetable, panel.WAVETABLES, "wavetable")
    from microfreak import mccfile
    if args.out:
        try:
            mccfile.check_ext(args.out, "wavetable")
        except mccfile.FileError as exc:
            ap.error(str(exc))

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        name, frames = read_wavetable(synth, args.slot, seqgen, verbose=True)
    finally:
        synth.close()

    if name is None:
        print(f"wavetable {args.slot + 1}: empty")
        return

    present = sum(1 for f in frames if f is not None)
    print(f"wavetable {args.slot + 1}: {name!r}  ({present}/4 frames present)")

    if args.out:
        if present != 4:
            sys.exit(f"wavetable {args.slot + 1} has {present} of 4 frames; "
                     f"not writing {args.out}")
        pcm = mccfile.wavetable_pcm(frames)
        mccfile.write_one(args.out, mccfile.audio_archive(name, pcm, "wavetable"),
                          "wavetable")
        print(f"wrote {args.out}")
    else:
        for i, data in enumerate(frames):
            if data is None:
                print(f"  frame {i}: empty")
                continue
            print(f"  frame {i}: {len(data)} bytes")
            for off in range(0, min(len(data), 64), 16):
                chunk = data[off:off + 16]
                h = " ".join(f"{b:02X}" for b in chunk)
                print(f"    {off:04d}  {h}")
            if len(data) > 64:
                print(f"    ... {len(data) - 64} more bytes")


if __name__ == "__main__":
    main()

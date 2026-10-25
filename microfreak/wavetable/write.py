#!/usr/bin/env python3
"""Upload a wavetable to the instrument, replacing what is there.

    mfreak wavetable encode pad.wav pad.mfwz
    mfreak wavetable write 3 --file pad.mfwz --yes
    mfreak wavetable write 5 --from 3 --yes          copy wavetable 3 onto 5

--file takes an MCC wavetable file, and its name unless --name gives
another. It shows what it would write and asks first. --yes skips the question. The result is read
back and checked unless --no-verify.
"""
import argparse
import sys

from microfreak import confirm

from microfreak.midi import open_synth, build, checksum_free_seq
from microfreak import jsonout
from microfreak import mccfile
from microfreak import panel
from microfreak.wavetable.read import (
    EMPTY_FLAG, FLAGS, NAME_START, STRAY_NULL_OFFSETS, is_empty, pull_block,
    read_wavetable, wavetable_name_of,
)

CHUNK_LEN = 32
FRAME_CHUNKS = 147
FRAME_LEN = CHUNK_LEN * FRAME_CHUNKS  # 4704
NAME_FIELD_LEN = 18  # payload[NAME_START:NAME_START+NAME_FIELD_LEN]

MARKER_08_17 = [0x08, 0x17, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]


def ack(synth, seq):
    synth.recv(lambda m: len(m) == 10 and m[7] == 0x00 and m[8] == 0x18 and m[6] == seq)


# Header bytes 0, 2-9, 11-13 observed constant across all 16 factory
# wavetable name chunks (only bytes 1 and 10, the slot-echo positions, and
# the name field itself vary) -- see microfreak-emu/docs/PROTOCOL.md. Used to build
# a name chunk from scratch, with no existing slot needed as a template.
NAME_CHUNK_CONSTANT_HEADER = [0x00, None, 0, 0, 0, 0, 0, 0, 0, 0, None, 0x00, 0x01, 0x01]


def build_name_chunk(hi, name, empty=False):
    """Builds a 32-byte wavetable name-chunk payload from scratch (no
    template needed) -- see NAME_CHUNK_CONSTANT_HEADER. empty=True flags the
    slot empty, as MCC does when it empties one.
    """
    header = bytearray(hi if b is None else b for b in NAME_CHUNK_CONSTANT_HEADER)
    if empty:
        header[FLAGS] = EMPTY_FLAG
    assert len(header) == NAME_START
    name_bytes = name.encode("ascii", errors="replace")
    max_len = NAME_FIELD_LEN - len(STRAY_NULL_OFFSETS)
    if len(name_bytes) > max_len:
        raise ValueError(f"name too long: max {max_len} ASCII characters")
    field = bytearray(NAME_FIELD_LEN)
    si = 0
    for i in range(NAME_FIELD_LEN):
        if i in STRAY_NULL_OFFSETS:
            continue
        if si < len(name_bytes):
            field[i] = name_bytes[si]
            si += 1
    return bytes(header) + bytes(field)


def patch_name_chunk(chunk_payload, hi, name=None):
    """Returns a new 32-byte name-chunk payload with the slot echoed at both
    known positions and (optionally) a new name, preserving the two stray
    null bytes microfreak-emu/docs/PROTOCOL.md documents at name-relative offsets 2
    and 10, the empty flag cleared, and every other byte exactly as read.
    """
    payload = bytearray(chunk_payload)
    payload[FLAGS] &= ~EMPTY_FLAG
    payload[1] = hi
    payload[10] = hi
    if name is not None:
        name_bytes = name.encode("ascii", errors="replace")
        max_len = NAME_FIELD_LEN - len(STRAY_NULL_OFFSETS)
        if len(name_bytes) > max_len:
            raise ValueError(f"name too long: max {max_len} ASCII characters")
        field = bytearray(NAME_FIELD_LEN)
        si = 0
        for i in range(NAME_FIELD_LEN):
            if i in STRAY_NULL_OFFSETS:
                field[i] = 0
            elif si < len(name_bytes):
                field[i] = name_bytes[si]
                si += 1
        payload[NAME_START:NAME_START + NAME_FIELD_LEN] = field
    return bytes(payload)


def write_name(synth, hi, name_chunk_payload, seqgen, verbose=False):
    seq = next(seqgen)
    synth.send(build(seq, 0x03, 0x56, hi, 0, 0x00))
    ack(synth, seq)

    seq = next(seqgen)
    synth.send(build(seq, 0x00, 0x15))
    ack(synth, seq)

    seq = next(seqgen)
    synth.send(build(seq, 0x20, 0x16, *name_chunk_payload))
    ack(synth, seq)

    seq = next(seqgen)
    synth.send(build(seq, *MARKER_08_17))
    ack(synth, seq)

    if verbose:
        print(f"  wrote name chunk + marker", file=sys.stderr)


def write_frame(synth, hi, lo, frame_data, seqgen, verbose=False):
    if len(frame_data) != FRAME_LEN:
        raise ValueError(f"frame must be exactly {FRAME_LEN} bytes, got {len(frame_data)}")

    seq = next(seqgen)
    synth.send(build(seq, 0x03, 0x54, hi, lo, 0x01))
    ack(synth, seq)

    seq = next(seqgen)
    synth.send(build(seq, 0x00, 0x15))
    ack(synth, seq)

    for i in range(FRAME_CHUNKS):
        chunk = frame_data[i * CHUNK_LEN:(i + 1) * CHUNK_LEN]
        sub = 0x17 if i == FRAME_CHUNKS - 1 else 0x16
        seq = next(seqgen)
        synth.send(build(seq, 0x20, sub, *chunk))
        ack(synth, seq)

    if verbose:
        print(f"  wrote frame {lo}: {FRAME_CHUNKS} chunks, {len(frame_data)} bytes",
              file=sys.stderr)


def write_wavetable(synth, dest_slot, name_chunk_payload, frames, seqgen, verbose=False):
    write_name(synth, dest_slot, name_chunk_payload, seqgen, verbose)
    for lo, frame_data in enumerate(frames):
        write_frame(synth, dest_slot, lo, frame_data, seqgen, verbose)


def read_name_chunk(synth, slot, seqgen):
    """Returns just the first (and only meaningfully-distinct) chunk of the
    name block, which is all write_name needs.
    """
    block = pull_block(synth, 0x57, slot, 0, seqgen)
    if block is None:
        return None
    return block[:32]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dest", type=int, metavar="WAVETABLE",
                    help="which wavetable to overwrite (1-16)")
    ap.add_argument("--from", dest="source", type=int, metavar="WAVETABLE",
                    help="copy name and frames from this wavetable")
    ap.add_argument("--file", metavar="FILE.mfwz",
                    help="an MCC wavetable file, e.g. from `wavetable encode`")
    ap.add_argument("--name", help="new name (up to 16 ASCII characters)")
    ap.add_argument("--yes", action="store_true",
                     help="write without asking")
    ap.add_argument("--no-verify", action="store_true",
                     help="skip reading it back afterwards")
    ap.add_argument("--port", default="MicroFreak", help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0, help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    args.dest_slot = panel.to_slot(ap, args.dest, panel.WAVETABLES, "wavetable")
    args.from_slot = panel.to_slot(ap, args.source, panel.WAVETABLES,
                                   "--from wavetable")
    if args.file and args.from_slot is not None:
        ap.error("--file and --from are alternatives")
    file_item = None
    if args.file:
        try:
            file_item = mccfile.read_one(args.file, "wavetable")
            if args.name is None:
                args.name = file_item.name
            mccfile.check_name(args.name, "wavetable")
        except mccfile.FileError as exc:
            sys.exit(str(exc))

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        if file_item is not None:
            frames = mccfile.wavetable_frames(file_item.pcm())
            name_chunk = build_name_chunk(args.dest_slot, args.name)
            source_slot = None
        else:
            source_slot = args.from_slot if args.from_slot is not None else args.dest_slot
            print(f"reading template from wavetable {source_slot + 1}...", file=sys.stderr)
            name_chunk = read_name_chunk(synth, source_slot, seqgen)
            if is_empty(name_chunk):
                sys.exit(f"error: wavetable {source_slot + 1} is empty, nothing to clone/round-trip")

            frames = []
            for lo in range(4):
                data = pull_block(synth, 0x55, source_slot, lo, seqgen, verbose=True,
                                   label=f"frame {lo}")
                if data is None:
                    sys.exit(f"error: wavetable {source_slot + 1} frame {lo} "
                              f"is empty, so the wavetable would be incomplete")
                frames.append(data)

            name_chunk = patch_name_chunk(name_chunk, args.dest_slot, args.name)

        final_name = wavetable_name_of(name_chunk)

        print(f"\nabout to write wavetable {args.dest_slot + 1}: {final_name!r} "
              f"(4 frames, {sum(len(f) for f in frames)} bytes)", file=sys.stderr)
        if source_slot is not None and source_slot != args.dest_slot:
            print(f"  (cloned from wavetable {source_slot + 1})", file=sys.stderr)
        elif args.file:
            print(f"  (from {args.file})", file=sys.stderr)

        if not confirm.ask("Overwrite wavetable %d?" % (args.dest_slot + 1),
                           args.yes):
            jsonout.result(slot=args.dest_slot, panel=args.dest_slot + 1,
                           name=final_name, written=False, verified=None)
            return

        write_wavetable(synth, args.dest_slot, name_chunk, frames, seqgen, verbose=True)
        print(f"wrote wavetable {args.dest_slot + 1}: {final_name!r}")
        jsonout.result(slot=args.dest_slot, panel=args.dest_slot + 1,
                       name=final_name, written=True, verified=None)

        if not args.no_verify:
            print("verifying...", file=sys.stderr)
            got_name, got_frames = read_wavetable(synth, args.dest_slot, seqgen)
            ok = got_name == final_name and all(
                got_frames[i] == frames[i] for i in range(4)
            )
            if ok:
                print("verify OK: read-back matches exactly")
                jsonout.result(verified=True)
            else:
                # Exits 1: a read-back that disagrees is a failed write, and
                # this used to report it and still exit 0.
                print("verify MISMATCH:", file=sys.stderr)
                print(f"  name: wrote {final_name!r}  read {got_name!r}",
                      file=sys.stderr)
                differed = []
                for i in range(4):
                    match = got_frames[i] == frames[i]
                    if not match:
                        differed.append(i)
                    print(f"  frame {i}: {'matches' if match else 'DIFFERS'}",
                          file=sys.stderr)
                jsonout.result(verified=False, read_back=got_name,
                               frames_differing=differed)
                return 1
    finally:
        synth.close()


if __name__ == "__main__":
    main()

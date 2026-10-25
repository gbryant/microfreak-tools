#!/usr/bin/env python3
"""Upload a sample to the instrument, replacing what is there.

    mfreak sample encode kick.wav kick.mfsz
    mfreak sample write 60 --file kick.mfsz --yes

--file takes an MCC sample file, and its name unless --name gives another.
It shows what it would write and asks first. --yes skips the question. The old sample cannot be read
back first, so it is gone for good unless you have the file. The name is
checked afterwards unless --no-verify.

The upload sequence is in microfreak-emu's docs/PROTOCOL.md, and the audio
format in docs/SAMPLE_ENCODING.md.
"""
import argparse
import sys

from microfreak.sample.encode import pack_7to8
from microfreak import panel
from microfreak.midi import open_synth, build, checksum_free_seq
from microfreak.wavetable.read import pull_block
from microfreak import confirm
from microfreak import jsonout
from microfreak import mccfile

CHUNK_LEN = 32
BLOCK_CHUNKS = 147     # max chunks per 03/58 select cycle
# The 32-byte name chunk is 7-to-8 packed like every other chunk on the wire:
# 4 groups of 8 wire bytes -> 28 real bytes. Decoded layout, established from
# captures/send-1-sample-bank.pcapng + captures/delete-sample.pcapng and
# cross-checked against MCC's own .mfsz export:
#
#   [0:4]    u32   0 in every chunk observed
#   [4:8]    u32   LE sample length in BYTES
#   [8:10]   2B    purpose unknown; 00 00 on empty slots, non-zero on real ones
#   [10:23]  13B   name, ASCII, NUL-terminated -- so 12 characters
#   [23]     u8    SLOT INDEX -- this is what the device actually routes on
#
# Those two lines used to read "[10:28] 18B name" and "[23] slot index", one
# directly under the other, and the overlap went unseen: byte 23 is inside
# 10..28. With an 18-character limit a 14-character name put its 14th
# character where the firmware reads the destination from, so
# `sample write 1 --name 'String Staccat'` wrote SAMPLE 117 instead --
# silently, with the write reporting success. A 13-character name left the
# slot byte alone but had no room for the NUL, so the read-back came back as
# the name with the slot byte stuck on the end and merely failed to verify.
#
# 12 is the limit, and three independent things agree on it: the slot byte at
# 23, the NUL a 13-byte field needs, and the device's own behaviour -- every
# one of the 52 names `sample list` measured came back at 12 or fewer.
#
# [4:8] is the reason writes to an empty slot silently failed. The old code
# reused a 12-byte header captured verbatim from one 'Piano Pop' write, which
# declares that sample's length (145324) for every sample written. On a slot
# MCC had already populated the directory entry carried a usable extent so it
# happened to work; on an empty slot the wrong length means the device never
# gets a valid allocation. See microfreak-emu/docs/PROTOCOL.md.
#
# Confirmed: 'bee buzz' decodes to length 371712, exactly the value in
# captures/sample-encoding/bee-buzz-exported-archive.txt.
LEN_OFFSET = 4
UNKNOWN_OFFSET = 8
NAME_OFFSET = 10
NAME_MAX = 12         # 12 characters + a NUL fills the 13-byte field at
                      # [10:23] without reaching the slot byte. See above.
SLOT_OFFSET = 23      # decoded byte 23 = slot index; the firmware reads the
                      # destination slot from HERE (ldrb r3,[r1,#23] at
                      # 0x08046708 / 0x08046790), not from the 03 5D/5A/58
                      # select argument. Leaving it 0 sends every write to
                      # slot 0 regardless of which slot you selected.
DECODED_CHUNK_LEN = 28


def decode_name_chunk(payload):
    """32 wire bytes -> the 28 decoded bytes."""
    out = bytearray()
    for g in range(0, len(payload) - 7, 8):
        header = payload[g]
        for i in range(7):
            out.append(payload[g + 1 + i] | (((header >> i) & 1) << 7))
    return bytes(out)


def sample_length_of(payload):
    """Sample length in bytes as the device reports it. 0 means empty."""
    d = decode_name_chunk(payload)
    return int.from_bytes(d[LEN_OFFSET:LEN_OFFSET + 4], "little")


def ack(synth, seq):
    synth.recv(lambda m: len(m) == 10 and m[7] == 0x00 and m[8] == 0x18 and m[6] == seq)


# mfreak sample encode emits whole 2048-sample blocks: each block is
# BLOCK_SAMPLES*2 = 4096 audio bytes plus BLOCK_PAD filler, packed 7-to-8 into
# BLOCK_CHUNKS * CHUNK_LEN = 4704 wire bytes. So the audio byte count is a
# function of the block count, NOT of the raw wire length -- 4704 wire bytes
# carry 4116 decoded bytes, only 4096 of which are audio.
WIRE_PER_BLOCK = BLOCK_CHUNKS * CHUNK_LEN   # 147 * 32 = 4704
AUDIO_PER_BLOCK = 4096                      # 2048 samples * 2 bytes


def decoded_len(wire_audio):
    """Wire bytes -> the audio byte count to declare in the name chunk.

    Returns the block-padded length, which is exactly what gets sent. MCC
    declares the pre-padding length instead ('bee buzz': 371712 vs the 372736
    actually transmitted, a 1024-byte / 16 ms difference of trailing silence).
    Either should be valid; this errs toward matching the bytes on the wire.
    Pass audio_len to build_name_chunk explicitly to override.
    """
    if len(wire_audio) % WIRE_PER_BLOCK:
        raise ValueError(f"wire audio must be a multiple of {WIRE_PER_BLOCK} "
                          f"bytes (one encoder block), got {len(wire_audio)}")
    return len(wire_audio) // WIRE_PER_BLOCK * AUDIO_PER_BLOCK


def build_name_chunk(name, audio_len=0, unknown=b"\x00\x00", slot=0):
    """Builds the 32-byte wire name chunk.

    audio_len is the sample's length in BYTES (the decoded length, not the
    wire length) and must match the audio actually sent; 0 marks the slot
    empty, which is what MCC writes to every untouched slot and what a delete
    sends.

    `unknown` is decoded bytes 8:9. Empty slots and deletes carry 00 00; real
    samples carry something non-zero that is not derivable from the two
    samples observed ('bee buzz' f5 2b, 'Piano Pop' a2 66) and is not a
    checksum over the audio (ruled out: sum, xor, adler, crc32, CCITT, XMODEM,
    MODBUS, ARC). Left settable rather than guessed at.
    """
    name_bytes = name.encode("ascii", errors="replace")
    if len(name_bytes) > NAME_MAX:
        raise ValueError(f"name too long: max {NAME_MAX} ASCII characters")
    if len(unknown) != 2:
        raise ValueError("unknown must be exactly 2 bytes")

    if not 0 <= slot <= 127:
        raise ValueError("slot must be 0-127")

    # The name must not be able to reach the slot byte, whatever the limit is
    # set to. This is the invariant the old limit violated, asserted rather
    # than commented because the two numbers are declared thirteen lines apart
    # and nobody noticed they overlapped.
    assert NAME_OFFSET + NAME_MAX <= SLOT_OFFSET, (
        "a %d-character name reaches byte %d, and the firmware reads the "
        "destination slot from byte %d"
        % (NAME_MAX, NAME_OFFSET + NAME_MAX - 1, SLOT_OFFSET))

    decoded = bytearray(DECODED_CHUNK_LEN)
    decoded[SLOT_OFFSET] = slot
    decoded[LEN_OFFSET:LEN_OFFSET + 4] = int(audio_len).to_bytes(4, "little")
    decoded[UNKNOWN_OFFSET:UNKNOWN_OFFSET + 2] = unknown
    decoded[NAME_OFFSET:NAME_OFFSET + len(name_bytes)] = name_bytes
    return pack_7to8(decoded)


def name_of_sample_chunk(chunk_payload):
    """The slot's name. Decodes the chunk properly rather than skipping fixed
    wire positions -- the old {4, 10} stray-offset guess put the second skip
    at wire byte 22, but the 7-to-8 group headers inside the name field are at
    wire bytes 16 and 24, so any name of 10 or more characters came out wrong.
    Every name in send-1-sample-bank.pcapng is 9 characters or fewer, which is
    why this never showed up.
    """
    d = decode_name_chunk(chunk_payload)
    return d[NAME_OFFSET:].split(b"\0")[0].decode("ascii", errors="replace")


def send_name_first(synth, slot, name_chunk, seqgen, verbose=False):
    seq = next(seqgen)
    synth.send(build(seq, 0x03, 0x5D, slot, 0, 0x00))
    ack(synth, seq)

    seq = next(seqgen)
    synth.send(build(seq, 0x00, 0x15))
    ack(synth, seq)

    seq = next(seqgen)
    synth.send(build(seq, 0x20, 0x17, *name_chunk))
    synth.recv(lambda m: len(m) == 11 and m[7] == 0x01 and m[8] == 0x16 and m[6] == seq)
    ack(synth, seq)
    if verbose:
        print("  sent name (03 5D)", file=sys.stderr)


def send_name_again(synth, slot, name_chunk, seqgen, verbose=False):
    seq = next(seqgen)
    synth.send(build(seq, 0x03, 0x5A, slot, 0, 0x00))
    ack(synth, seq)

    seq = next(seqgen)
    synth.send(build(seq, 0x00, 0x15))
    ack(synth, seq)

    seq = next(seqgen)
    synth.send(build(seq, 0x20, 0x17, *name_chunk))
    ack(synth, seq)
    if verbose:
        print("  sent name (03 5A)", file=sys.stderr)


def send_audio(synth, slot, audio, seqgen, verbose=False):
    """A slot holds ONE sample, but each 03/58 select only covers up to
    BLOCK_CHUNKS (147) chunks -- a longer sample is sent as multiple
    sequential select+block cycles, each continuing where the last left off
    (confirmed against the 'Vinyl' sample: 41 reselects, 147 chunks each,
    6027 chunks / 192864 bytes total). Reselecting doesn't restart the
    sample; there's no per-block addressing, purely sequential continuation.
    """
    if len(audio) % CHUNK_LEN != 0 or len(audio) == 0:
        raise ValueError(f"audio length must be a positive multiple of {CHUNK_LEN}, "
                          f"got {len(audio)}")
    total_chunks = len(audio) // CHUNK_LEN

    pos = 0
    block_num = 0
    while pos < total_chunks:
        n = min(BLOCK_CHUNKS, total_chunks - pos)

        seq = next(seqgen)
        synth.send(build(seq, 0x03, 0x58, slot, 0, 0x01))
        ack(synth, seq)

        seq = next(seqgen)
        synth.send(build(seq, 0x00, 0x15))
        ack(synth, seq)

        for i in range(n):
            idx = pos + i
            chunk = audio[idx * CHUNK_LEN:(idx + 1) * CHUNK_LEN]
            sub = 0x17 if i == n - 1 else 0x16
            seq = next(seqgen)
            synth.send(build(seq, 0x20, sub, *chunk))
            ack(synth, seq)

        pos += n
        block_num += 1
        if verbose:
            print(f"  block {block_num}: {n} chunks", file=sys.stderr)

    if verbose:
        print(f"  sent {block_num} blocks, {total_chunks} chunks total, "
              f"{len(audio)} bytes", file=sys.stderr)


def write_sample(synth, slot, name, audio, seqgen, verbose=False):
    # The declared length must match the audio actually sent, or the device
    # never gets a usable allocation for the slot (microfreak-emu/docs/PROTOCOL.md).
    name_chunk = build_name_chunk(name, audio_len=decoded_len(audio), slot=slot)
    send_name_first(synth, slot, name_chunk, seqgen, verbose)
    send_name_again(synth, slot, name_chunk, seqgen, verbose)
    send_audio(synth, slot, audio, seqgen, verbose)


def read_slot_record(synth, slot, seqgen):
    """(name, audio_bytes, flash_address) for a slot, via the 03 5B read-back.

    The read-back is the sample's whole record header, not just its name:

        +0x00  u32  the record's own address in the external flash
        +0x04  u32  the audio length in bytes
        +0x08  u16  the content hash the granular engines gate on
        +0x0A  13B  name
        +0x17  u8   slot index, 0xFF when the slot is empty

    The address is what makes the library's extent knowable: the records are
    a contiguous chain, so the last one's address plus its page-rounded size
    is where the used space ends. Confirmed on 5.0.0.2084 -- 52 records, no
    break, the first at 0x281000.
    """
    block = pull_block(synth, 0x5B, slot, 0, seqgen)
    if block is None:
        return None, None, None
    d = decode_name_chunk(block[:32])
    return (name_of_sample_chunk(block[:32]),
            int.from_bytes(d[4:8], "little"),
            int.from_bytes(d[0:4], "little"))


def read_slot_meta(synth, slot, seqgen):
    """(name, declared_length) for a slot, via the 03 5B read-back.

    declared_length is the sample's byte count as the device holds it; 0 means
    the slot is empty. This is the only read-back that shows whether a write
    actually allocated the slot -- the audio itself cannot be read back.
    """
    block = pull_block(synth, 0x5B, slot, 0, seqgen)
    if block is None:
        return None, None
    payload = block[:32]
    return name_of_sample_chunk(payload), sample_length_of(payload)


def verify_slot_name(synth, slot, seqgen):
    """Uses the 03 5B read-back mechanism (microfreak-emu/docs/PROTOCOL.md) to check
    the slot's name landed. This does NOT verify the audio data — there is
    no observed way to read that back.
    """
    name, _ = read_slot_meta(synth, slot, seqgen)
    return name


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sample", type=int, metavar="SAMPLE",
                    help="which sample to overwrite (1-128)")
    ap.add_argument("--file", required=True, metavar="FILE.mfsz",
                    help="an MCC sample file, e.g. from `sample encode`")
    ap.add_argument("--name", help="a new name, up to 12 ASCII characters "
                    "(default: the file's)")
    ap.add_argument("--yes", action="store_true",
                     help="write without asking")
    ap.add_argument("--no-verify", action="store_true",
                     help="skip checking the name afterwards")
    ap.add_argument("--port", default="MicroFreak", help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0, help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    # Panel number in, 0-based slot out, straight after parsing so
    # everything below still sees the index it always did.
    args.slot = panel.to_slot(ap, args.sample, panel.SAMPLES, "sample")

    try:
        item = mccfile.read_one(args.file, "sample")
        if args.name is None:
            args.name = item.name
        mccfile.check_name(args.name, "sample")
    except mccfile.FileError as exc:
        sys.exit(str(exc))
    audio = mccfile.sample_wire(item.pcm())

    n_blocks = (len(audio) // CHUNK_LEN + BLOCK_CHUNKS - 1) // BLOCK_CHUNKS
    print(f"about to write sample {args.slot + 1}: {args.name!r} "
          f"({len(audio)} bytes, {len(audio) // CHUNK_LEN} chunks, {n_blocks} blocks)",
          file=sys.stderr)
    print("WARNING: this replaces the sample in this slot, and the old one "
          "cannot be read back.", file=sys.stderr)

    if not confirm.ask("Overwrite sample %d?" % (args.slot + 1), args.yes):
        jsonout.result(slot=args.slot, panel=args.slot + 1, name=args.name,
                       bytes=len(audio), written=False, verified=None)
        return

    synth = open_synth(args.port, timeout=args.timeout)
    seqgen = checksum_free_seq()
    try:
        write_sample(synth, args.slot, args.name, audio, seqgen, verbose=True)
        print(f"wrote sample {args.slot + 1}: {args.name!r}")
        jsonout.result(slot=args.slot, panel=args.slot + 1, name=args.name,
                       bytes=len(audio), written=True, verified=None)

        if not args.no_verify:
            print("verifying name (audio data cannot be verified)...", file=sys.stderr)
            got_name = verify_slot_name(synth, args.slot, seqgen)
            if got_name == args.name:
                print("verify OK: name matches")
                jsonout.result(verified=True)
            else:
                # A mismatch is a FAILED write, so it exits 1. It used to
                # print this and exit 0, which made a confirmed bad write
                # look clean to anything reading the exit code.
                jsonout.result(verified=False, read_back=got_name)
                print(f"verify MISMATCH: wrote {args.name!r}, "
                      f"read back {got_name!r}", file=sys.stderr)
                return 1
    finally:
        synth.close()


if __name__ == "__main__":
    main()

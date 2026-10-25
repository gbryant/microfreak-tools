"""MIDI Control Center's files, and the bytes the instrument takes for them.

Every command that reads or writes a preset, sample or wavetable file goes
through here, so the only files a user handles are MCC's own:

    one item     .mfpz (preset)    .mfsz (sample)    .mfwz (wavetable)
    a bank       .mfprojz or .mfbz .mfsbz            .mfwbz

A single-item file is a ZIP holding one Boost text archive named
`0_<name>`. A bank is a ZIP of them, one per slot; bank.py reads and writes
those. The archive's array is the instrument's internal form of the item:

    preset      the 4672 wire bytes, as sent and received over SysEx
    sample      16-bit LE PCM at 32 kHz, mono
    wavetable   16-bit LE PCM, 8192 samples (4 frames of 2048)

Samples and wavetables go over the wire in blocks of 2048 samples plus 20
zero bytes, 7-to-8 packed (docs/SAMPLE_ENCODING.md). The functions below
convert between the two.
"""
import os
import re
import zipfile

import microfreak.bank as B
from microfreak.sample.encode import BLOCK_PAD, BLOCK_SAMPLES, decode_7to8, pack_7to8

SINGLE = {".mfpz": "preset", ".mfsz": "sample", ".mfwz": "wavetable"}
BANKS = {".mfprojz": "preset", ".mfbz": "preset", ".mfsbz": "sample",
         ".mfwbz": "wavetable"}
SINGLE_EXT = {kind: ext for ext, kind in SINGLE.items()}

PRESET_VERSION = "48"     # what the 5.0 factory bank uses for all 512 presets
AUDIO_VERSION = "134"     # what MCC writes on a sample or wavetable export
NAME_LIMIT = {"preset": 14, "sample": 12, "wavetable": 16}

BLOCK_BYTES = BLOCK_SAMPLES * 2              # 4096 bytes of audio per block
WIRE_BLOCK = (BLOCK_BYTES + BLOCK_PAD) // 7 * 8   # 4704
WAVETABLE_FRAMES = 4
WAVETABLE_BYTES = WAVETABLE_FRAMES * BLOCK_BYTES  # 16384


class FileError(Exception):
    """A file that is not the MCC file a command needs. The message says why."""


def kind_of(path):
    """-> ("preset" | "sample" | "wavetable", is_bank) from the extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext in SINGLE:
        return SINGLE[ext], False
    if ext in BANKS:
        return BANKS[ext], True
    raise FileError("%s is not an MCC file. Presets are .mfpz, samples .mfsz, "
                    "wavetables .mfwz; banks are .mfprojz, .mfbz, .mfsbz and "
                    ".mfwbz." % path)


def check_ext(path, kind, bank=False):
    """Refuse a path whose extension is not this kind of file."""
    want = ([e for e, k in BANKS.items() if k == kind] if bank
            else [SINGLE_EXT[kind]])
    if os.path.splitext(path)[1].lower() not in want:
        raise FileError("%s: a %s %s is %s" % (
            path, kind, "bank" if bank else "file", " or ".join(want)))


def check_name(name, kind):
    limit = NAME_LIMIT[kind]
    if len(name.encode("ascii", "replace")) > limit:
        raise FileError("the name %r is longer than %d characters, the most "
                        "a %s can have" % (name, limit, kind))


# ---------------------------------------------------------------- single items

def read_one(path, kind):
    """-> bank.Item for a .mfpz / .mfsz / .mfwz, checked for `kind`."""
    check_ext(path, kind)
    if not os.path.isfile(path):
        raise FileError("%s: no such file" % path)
    try:
        item = B.read_item(path)
    except (zipfile.BadZipFile, ValueError, IndexError) as exc:
        raise FileError("%s: not a readable MCC file. %s" % (path, exc))
    _check_values(item, kind, path)
    return item


def write_one(path, archive, kind):
    """Write one archive as a single-item MCC file."""
    check_ext(path, kind)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("0_%s" % _member_safe(archive.name),
                   B.build_archive(archive).encode("latin-1"))
    return path


def _member_safe(name):
    return re.sub(r"[/\\]", "_", name).strip() or "item"


def _check_values(item, kind, path):
    n = len(item.values)
    if kind == "preset" and n != B.PRESET_WIRE_LEN:
        raise FileError("%s holds %d values, not a preset's %d"
                        % (path, n, B.PRESET_WIRE_LEN))
    if kind == "wavetable" and n != WAVETABLE_BYTES:
        raise FileError("%s holds %d values, not a wavetable's %d"
                        % (path, n, WAVETABLE_BYTES))
    if kind == "sample" and (n < 2 or n % 2):
        raise FileError("%s holds %d values, not 16-bit audio" % (path, n))


# ------------------------------------------------------------------- archives

def preset_archive(name, category, wire, schema=None):
    """An archive for a preset, as MCC writes one. `schema` is the byte the
    instrument stored with it; without one it is derived from the payload,
    which is right only for a preset saved by 5.0 firmware."""
    check_name(name, "preset")
    if schema is None:
        schema = B.schema_byte(wire)
    return B.Archive(version=PRESET_VERSION, name=name, tags=B.tag_string(),
                     values=list(_signed(wire)),
                     scalars=(str(category), "0", "0"),
                     abc=("0", "0", str(schema)))


def empty_archive(name):
    """An unused preset slot, as MCC writes one: no payload."""
    return B.empty_preset(name)


def audio_archive(name, pcm, kind):
    """An archive for a sample or wavetable, as MCC writes one."""
    check_name(name, kind)
    return B.Archive(version=AUDIO_VERSION, name=name, tags=B.tag_string(),
                     values=list(_signed(pcm)), scalars=("1", "0", "0"),
                     abc=("0", "0", "1"))


def preset_of(item):
    """-> (name, category, wire bytes) for a preset item."""
    return item.name, int(item.archive.scalars[0]), item.wire()


def _signed(data):
    return (b - 256 if b >= 128 else b for b in bytes(data))


# ---------------------------------------------------------------- audio <-> wire

def pcm_from_s16(s16):
    """int16 samples -> 16-bit LE PCM bytes."""
    return s16.astype("<i2").tobytes()


def _blocks_to_wire(pcm):
    out = bytearray()
    for off in range(0, len(pcm), BLOCK_BYTES):
        block = pcm[off:off + BLOCK_BYTES]
        block = block + bytes(BLOCK_BYTES - len(block) + BLOCK_PAD)
        out += pack_7to8(block)
    return bytes(out)


def _wire_to_blocks(wire):
    out = bytearray()
    for off in range(0, len(wire), WIRE_BLOCK):
        real = decode_7to8(list(wire[off:off + WIRE_BLOCK]))
        out += bytes(b & 0xFF for b in real[:BLOCK_BYTES])
    return bytes(out)


def sample_wire(pcm):
    """Sample PCM -> the wire bytes `sample write` sends. The last block is
    padded with silence to a whole 2048 samples."""
    return _blocks_to_wire(pcm)


def sample_pcm(wire, length=None):
    """Wire bytes -> sample PCM, cut to `length` bytes if given."""
    pcm = _wire_to_blocks(wire)
    return pcm if length is None else pcm[:length]


def wavetable_frames(pcm):
    """Wavetable PCM (16384 bytes) -> the four 4704-byte wire frames."""
    if len(pcm) != WAVETABLE_BYTES:
        raise ValueError("a wavetable is %d bytes, got %d"
                         % (WAVETABLE_BYTES, len(pcm)))
    wire = _blocks_to_wire(pcm)
    return [wire[i * WIRE_BLOCK:(i + 1) * WIRE_BLOCK]
            for i in range(WAVETABLE_FRAMES)]


def wavetable_pcm(frames):
    """The four wire frames -> wavetable PCM."""
    return _wire_to_blocks(b"".join(frames))

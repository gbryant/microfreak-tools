#!/usr/bin/env python3
"""Named preset parameter table.

CONTROL/SWITCH are ported from francoisgeorgy/microfreak-reverse
(src/model/index.js, MIT-licensed sysex reverse-engineering notes) -- see
microfreak-emu's RELATED_PROJECTS.md -- and hardware-verified live, with one
stale sub-table (oscillator engine names) corrected from real hardware.

Mod-matrix cells are NOT addressed this way. They are looked up by name
through the TLV stream (`preset.tlv`) and decoded as one 24-bit fixed-point
value, both confirmed against the hardware display; the earlier fixed-byte
tables and per-byte value formula were wrong and have been removed. See
microfreak-emu/docs/FORMATS.md.

Addressing is [chunk, byte_offset]: `chunk` is the index of a preset's 32-byte
transport chunk (0..145, the same chunks `preset.read`'s read_preset()
already extracts as `chunk[9:41]` per reply -- raw wire bytes, NOT run through
our decode_7to8). `byte_offset` is 0..31 within that chunk. Every 32-byte
chunk is exactly four 8-byte MIDI 7-to-8 groups, so a group's header byte
always lands at a chunk-relative offset of 0, 8, 16, or 24 -- this table
reads specific bits directly out of that header byte rather than fully
decoding the block first, which is mathematically equivalent to our own
decode_7to8 (see docs/SAMPLE_ENCODING.md) but addressed per-field.

A preset payload here is represented as `rows`: a list of 146 bytes-like
objects, 32 bytes each (e.g. `rows_from_payload(read_preset(...)[1])`).
"""

CHUNK_PAYLOAD_LEN = 32
CHUNKS_PER_PRESET = 146


def rows_from_payload(payload):
    """Reshapes a flat 4672-byte raw (un-decoded) preset payload -- exactly
    what `preset.read`'s read_preset() returns -- into 146 rows of 32 bytes."""
    if len(payload) != CHUNK_PAYLOAD_LEN * CHUNKS_PER_PRESET:
        raise ValueError(f"expected {CHUNK_PAYLOAD_LEN * CHUNKS_PER_PRESET} bytes, "
                          f"got {len(payload)}")
    return [bytearray(payload[i * CHUNK_PAYLOAD_LEN:(i + 1) * CHUNK_PAYLOAD_LEN])
            for i in range(CHUNKS_PER_PRESET)]


def payload_from_rows(rows):
    if len(rows) != CHUNKS_PER_PRESET or any(len(r) != CHUNK_PAYLOAD_LEN for r in rows):
        raise ValueError("rows must be 146 rows of 32 bytes each")
    out = bytearray()
    for r in rows:
        out.extend(r)
    return bytes(out)


def _right_shift(mask):
    """Index of the lowest set bit in mask, i.e. the shift needed to bring
    it to bit 0. Mirrors bits-utils.js's getRightShift."""
    if not mask:
        return -1
    i = 0
    while (mask >> i) & 1 == 0:
        i += 1
    return i


DEFAULT_MSB_MASK = 0x01
DEFAULT_SIGN_MASK = 0x02


def multibytes_value(msb_byte, lsb_byte, msb_bit_byte, mask_msb, sign_byte=0, mask_sign=0):
    """Reconstructs a 15-bit (optionally signed) raw value from three (or
    four) already-received-as-7-bit wire bytes. Ported verbatim from
    model/index.js's multibytesValue."""
    sign_bit = 0
    if mask_sign:
        sign_bit = (sign_byte >> _right_shift(mask_sign)) & 1
    msb_bit = (msb_bit_byte >> _right_shift(mask_msb)) & 1
    high = (msb_byte & 0x7F) << 8
    mid = lsb_byte & 0x7F
    low = msb_bit << 7
    n = high + mid + low
    if sign_bit:
        return -(((~n) & 0x7FFF) + 1)
    return n


def raw_to_display(raw):
    return round(raw * 1000 / 32768) / 10


def display_to_raw(display, signed=False):
    """Inverse of raw_to_display: display*10 ~= raw*1000/32768, so
    raw ~= display*32768/100. Clamped to the 15-bit magnitude range (0x7FFF)
    since display=100.0 maps just past it (32768) before clamping."""
    raw = round(display * 32768 / 100)
    limit = 0x7FFF
    if signed:
        return max(-limit, min(limit, raw))
    return max(0, min(limit, raw))


def encode_multibytes(raw, mask_msb, mask_sign=0):
    """Inverse of multibytes_value: given a raw value (15-bit magnitude,
    optionally negative when mask_sign is set), returns
    (msb_byte, lsb_byte, msb_bit, sign_bit) -- the four component values to
    write back into the wire bytes (the caller ORs/ANDs msb_bit and sign_bit
    into the correct header-byte bit position; see set_field_raw below)."""
    sign_bit = 0
    n = raw
    if mask_sign:
        if raw < 0:
            sign_bit = 1
            n = ((~(-raw - 1)) & 0x7FFF)
        else:
            n = raw
    if not (0 <= n <= 0x7FFF):
        raise ValueError(f"raw value {raw} out of 15-bit range")
    msb_byte = (n >> 8) & 0x7F
    msb_bit = (n >> 7) & 0x01
    lsb_byte = n & 0x7F
    return msb_byte, lsb_byte, msb_bit, sign_bit


def _set_masked_bit(rows, chunk, offset, mask, bit_value):
    shift = _right_shift(mask)
    b = rows[chunk][offset]
    b &= ~mask & 0xFF
    if bit_value:
        b |= mask
    rows[chunk][offset] = b


# --- TLV addressing for CONTROL/SWITCH ---------------------------------
#
# Every field maps to a named record in the TLV stream. Looking cells up by
# name is layout-independent; the fixed [chunk, offset] tables are not. The
# corpus shows exactly how badly: whether a table entry is correct is a
# property of the PRESET, not the field -- filter_resonance, glide and
# cycling_env_amount agree on precisely the same 332 presets, and
# envelope_attack and lfo_rate_free on precisely the same 139. Different
# presets are laid out differently and a fixed table can only match one
# layout. See microfreak-emu/docs/FORMATS.md.
FIELD_TLV = {
    # CONTROL
    "osc_type": ("VCO", "Type"), "osc_wave": ("VCO", "Param1"),
    "osc_timbre": ("VCO", "Param2"), "osc_shape": ("VCO", "Param3"),
    "filter_cutoff": ("VCF", "Cutoff"), "filter_resonance": ("VCF", "Reso"),
    "cycling_env_rise": ("EG1", "RiseLvl"), "cycling_env_fall": ("EG1", "FallLvl"),
    "cycling_env_hold": ("EG1", "Hold"), "cycling_env_amount": ("EG1", "Amount"),
    "cycling_env_rise_shape": ("EG1", "RiseSlp"),
    "cycling_env_fall_shape": ("EG1", "FallSlp"),
    "glide": ("Kbd", "Glide"),
    "envelope_attack": ("EG2", "Attack"), "envelope_decay": ("EG2", "DecRel"),
    "envelope_sustain": ("EG2", "Sustain"),
    "arp_seq_rate_free": ("Arp", "Rate"), "arp_seq_rate_sync": ("Arp", "Div"),
    "arp_seq_swing": ("Arp", "Swing"), "spice": ("Arp", "Spice"),
    "lfo_rate_free": ("LFO", "Rate"), "lfo_rate_sync": ("LFO", "Div"),
    # SWITCH
    "filter_type": ("VCF", "Type"), "cycling_env_mode": ("EG1", "Mode"),
    "amp_mod": ("EG2", "Mode"), "octave": ("Kbd", "Octave"),
    "arp": ("Arp", "Enable"), "seq": ("Arp", "SeqOn"),
    "arp_seq_mod": ("Arp", "Dir"), "arp_seq_sync": ("Arp", "Sync"),
    "lfo_shape": ("LFO", "Shape"), "lfo_sync": ("LFO", "Sync"),
    "paraphonic": ("Gen", "Parafon"),
}

# RETRACTED 2026-09-10: osc_type was read as the record's high byte alone.
# It is the full 15-bit pair like every other CONTROL field -- the low byte
# carries real information and discarding it was the root of the engine
# mis-decode.  See engine_index_from_value() for the hardware evidence.
_SINGLE_BYTE_FIELDS = set()

def _tlv_records(rows):
    from microfreak.preset.tlv import decode_7to8, parse
    return decode_7to8(payload_from_rows(rows))


def _find_field_record(block, module, field):
    from microfreak.preset.tlv import parse, T_MODULE, T_NAME, T_VALUE
    records, end = parse(block)
    mod = pending = None
    for i, r in enumerate(records):
        if r.type == T_MODULE and r.length:
            mod = r.text()
        elif r.type == T_NAME and r.length:
            pending = r.text()
        elif r.type == T_VALUE and pending is not None:
            if mod == module and pending == field:
                return records, end, i
            pending = None
    raise KeyError("no record %s/%s in this preset" % (module, field))


_FIELD_BY_ID = None


def _field_name(field_def):
    """CONTROL/SWITCH entries are identified by object identity, so the public
    get/set signatures stay unchanged. Built lazily: FIELD_TLV is declared
    above the tables it refers to."""
    global _FIELD_BY_ID
    if _FIELD_BY_ID is None:
        _FIELD_BY_ID = {id(fd): n for table in (CONTROL, SWITCH)
                        for n, fd in table.items() if n in FIELD_TLV}
    return _FIELD_BY_ID.get(id(field_def))


def get_field_raw(rows, field_def):
    """field_def: a CONTROL or SWITCH entry. Reads via the TLV stream when the
    field is in FIELD_TLV, else falls back to the old [chunk, offset] path."""
    name = _field_name(field_def)
    if name is None:
        return _get_field_raw_wire(rows, field_def)
    block = _tlv_records(rows)
    records, _, idx = _find_field_record(block, *FIELD_TLV[name])
    body = records[idx].body
    if name in _SINGLE_BYTE_FIELDS:
        return body[2]
    # 15-bit, matching multibytes_value: the high byte is masked to 7 bits and
    # the low byte already carries the borrowed bit in its top position.
    return ((body[2] & 0x7F) << 8) | body[1]


def set_field_raw(rows, field_def, raw):
    """Writes a raw value back. TLV-addressed for fields in FIELD_TLV."""
    name = _field_name(field_def)
    if name is None:
        return _set_field_raw_wire(rows, field_def, raw)
    from microfreak.preset.tlv import serialize
    from microfreak.sample.encode import pack_7to8
    block = _tlv_records(rows)
    records, end, idx = _find_field_record(block, *FIELD_TLV[name])
    body = bytearray(records[idx].body)
    if name in _SINGLE_BYTE_FIELDS:
        body[2] = raw & 0xFF
    else:
        body[1] = raw & 0xFF
        body[2] = (body[2] & 0x80) | ((raw >> 8) & 0x7F)
    records[idx].body = bytes(body)
    rebuilt = serialize(records) + block[end:]
    if len(rebuilt) != len(block):
        raise ValueError("rebuild changed block length")
    rows[:] = rows_from_payload(pack_7to8(rebuilt))


def _get_field_raw_wire(rows, field_def):
    """The original fixed [chunk, offset] reading. Layout-dependent -- kept
    only for fields not yet in FIELD_TLV."""

    """field_def: a CONTROL or SWITCH entry (dict with MSB/LSB/msb keys)."""
    if field_def.get("MSB") is None:
        return rows[field_def["LSB"][0]][field_def["LSB"][1]]
    mask_msb = field_def["msb"][2] if len(field_def["msb"]) == 3 else DEFAULT_MSB_MASK
    return multibytes_value(
        rows[field_def["MSB"][0]][field_def["MSB"][1]],
        rows[field_def["LSB"][0]][field_def["LSB"][1]],
        rows[field_def["msb"][0]][field_def["msb"][1]],
        mask_msb,
    )



def _set_field_raw_wire(rows, field_def, raw):
    """The original fixed [chunk, offset] write. Layout-dependent."""
    """In-place patch of rows for a CONTROL or SWITCH entry (no sign bit --
    matches controlValue()/switchValue(), which never pass mask_sign)."""
    if field_def.get("MSB") is None:
        chunk, offset = field_def["LSB"]
        rows[chunk][offset] = raw & 0x7F
        return
    mask_msb = field_def["msb"][2] if len(field_def["msb"]) == 3 else DEFAULT_MSB_MASK
    msb_byte, lsb_byte, msb_bit, _ = encode_multibytes(raw, mask_msb)
    mc, mo = field_def["MSB"]
    lc, lo = field_def["LSB"]
    rows[mc][mo] = msb_byte
    rows[lc][lo] = lsb_byte
    _set_masked_bit(rows, field_def["msb"][0], field_def["msb"][1], mask_msb, msb_bit)


def switch_name(raw, values):
    """values: list of {'name': str, 'value': int}, ascending. Mirrors
    State.switchValue's `if (raw <= entry.value) return entry.name`."""
    for entry in values:
        if raw <= entry["value"]:
            return entry["name"]
    return raw


def switch_raw(name, values):
    for entry in values:
        if entry["name"] == name:
            return entry["value"]
    raise ValueError(f"{name!r} not one of {[e['name'] for e in values]}")


# Oscillator engines, live-hardware-corrected 2026-09-03: prior art's table
# (12 engines: RELATED_PROJECTS.md) is stale -- this firmware has 22. Confirmed via
# a real write/read: raw byte 64 reads back as "Speech" on the device.
# 12-engine table's raw bytes fit round((i+1)*127/N) for N=12 almost exactly
# (e.g. i=9 -> round(10*127/12)=106, matches prior art's 106 for Chords), and
# that SAME formula for N=22 gives round(11*127/22)=64 for index 10 (Speech,
# 11th in list) -- exactly the confirmed data point. So: still evenly spaced
# across 0-127, just more engines now. Only that one raw value (64->Speech)
# is directly hardware-confirmed; the rest are interpolated from the same
# formula, not individually verified.
_OSC_TYPE_ENGINES = [
    "Basic Waves", "Superwave", "Wavetable", "Harmonic", "KarplusStrong",
    "V. Analog", "Waveshaper", "Two Op. FM", "Formant", "Chords", "Speech",
    "Modal", "Noise", "Bass", "SawX", "Harm", "WaveUser", "Sample",
    "Scan Grains", "Cloud Grains", "Hit Grains", "Vocoder",
]
_OSC_TYPE_VALUES = {name: round((i + 1) * 127 / len(_OSC_TYPE_ENGINES))
                     for i, name in enumerate(_OSC_TYPE_ENGINES)}
_OSC_TYPE_NAMES = {v: k for k, v in _OSC_TYPE_VALUES.items()}


def _osc_type(raw):
    """Nearest-center lookup against the FULL 22-engine table.

    Count-blind, and therefore wrong for any preset that was not saved by a
    22-engine firmware -- see engine_from() and osc_engine(), which are what
    you want. Kept because the raw<->display round trip in CONTROL uses it.
    """
    return min(_OSC_TYPE_ENGINES,
               key=lambda name: abs(raw - _OSC_TYPE_VALUES[name]))


# Engine index <-> the VCO/Type 15-bit value, measured on hardware 2026-09-10.
#
# RETRACTED: engine_from(count, raw) scaled the engine table by byte 0 of the
# record, on the theory that a preset saved by a 12-engine firmware should be
# read against a 12-engine table.  The device does not do that.  It decodes
# every preset against its OWN engine list regardless of what byte 0 says,
# which is exactly why a preset written by older firmware displays as a
# different engine on a newer one -- the stored value is a *fraction* of the
# engine list, so the same fraction lands on a different engine when the list
# grows.  Byte 0 records the saving firmware's engine count but is not used
# to rescale on read.
#
# Ground truth: five presets written to a 5.0.0.2084 device and read off the
# panel, then re-saved by the device with the engine corrected and read back
# -- nine (value -> engine) pairs whose engine name came from the panel.
# Those nine uniquely determine the formula below; no other floor/round/ceil
# form over divisors 18..25 fits them.
#
#   v=10922 -> Waveshaper(6)   v=11915 -> Two Op. FM(7)
#   v=21845 -> Bass(13)        v=16384 -> Speech(10)
#   v=30036 -> Cloud Grains(19) v=7447 -> KarplusStrong(4)
#   v=13653 -> Formant(8)      v=14894 -> Chords(9)
#   v=27306 -> Sample(17)
#
# That confirms nine of the 22 table entries against hardware, up from two.
# The rest of the names remain interpolated and unverified.
# CORRECTED 2026-09-10 (second pass). The first model here was
# `floor(value * 21 / 32768)` with an encode of `floor((i+1) * 32768 / 22)`.
# Both agree with the corrected model for indices 0..16 and DIVERGE from 17
# up -- every engine confirmed at the time was <= 16, so the extrapolation
# looked perfect until a write intended as `Sample` (17) came back off the
# panel as `WaveUser` (16). Refitting against the panel-confirmed points gave
# a plain bin division -- also since retracted:
#
#     index = value // ENGINE_BIN_WIDTH
#
# The eleven points constrain the width to 1577.06 < w <= 1580.84; 1579 sits
# in the middle and fits all of them. Encoding therefore uses the bin CENTRE,
# which is safe anywhere in that interval -- do not reuse the old
# `(i+1)*32768/22` form, which lands on a boundary and is wrong above 16.
#
# Consequence of that retracted model: bin 21 starts at 33159, past the 15-bit
# maximum, so it made only indices 0..20 look selectable -- "21 engines, not
# 22". That conclusion is WRONG and was the model's fault, not the device's.
# All 22 entries are real: `Vocoder` stores 20852, a value the device itself
# wrote, and 0x5174 is the literal the firmware's audio-input path compares
# against (microfreak-emu/docs/FIRMWARE.md, "The audio-in consumer is gated on
# the raw value 20852"). Storage order is not display order, which is what
# broke every fit. Decode with ENGINE_VALUES below, never with a formula.
ENGINE_BIN_WIDTH = 1579
ENGINE_MAX_INDEX = 32767 // ENGINE_BIN_WIDTH     # 20
# What the device writes into byte 0 of the VCO/Type record, and also the
# number of engines ENGINE_VALUES holds: indices 0..21, every one measured
# off hardware and every one selectable. This comment used to say "only
# 0..20 are actually selectable", which was ENGINE_MAX_INDEX leaking out of
# the RETRACTED bin-division model above -- 32767 // 1579 is 20, so the
# model simply could not address the 22nd engine. `Hit Grains` is index 21
# at the saturated value 32767.
ENGINE_COUNT_BYTE = 22


def engine_index_from_value(value):
    """RETRACTED -- the bin-division model. Do not decode with this.

    Superseded 2026-09-10 by the measured ENGINE_VALUES table, and left here
    only because the docs describe it. It disagrees with hardware for the
    four engines stored at indices 13..16: the device wrote 20852 when
    `Vocoder` was selected and 22341 for `Bass`, which this puts in adjacent
    bins 13 and 14 while the knob shows them at display positions 21 and 13.
    Storage order is not display order, so no monotonic function of the value
    can be right. Use engine_from_value().
    """
    return max(0, min(len(_OSC_TYPE_ENGINES) - 1, value // ENGINE_BIN_WIDTH))


def engine_value_for_index(index):
    """A 15-bit value the device will read back as `index`.

    The centre of the bin, not the device's own written value -- centres are
    correct for any width in the measured interval, boundaries are not.
    """
    if index > ENGINE_MAX_INDEX:
        raise ValueError("engine index %d is not selectable (max %d)"
                         % (index, ENGINE_MAX_INDEX))
    return int((index + 0.5) * ENGINE_BIN_WIDTH)


def engine_from(count, raw):
    """DEPRECATED -- kept so old callers resolve. `count` is ignored.

    The old signature took the high byte only; callers that still pass it get
    a best-effort answer by treating `raw` as the high byte of the pair.
    """
    return engine_index_from_value((raw & 0x7F) << 8)


def osc_engine(rows):
    """The engine name this preset displays on a 22-engine device.

    FIXED 2026-09-11: this read through engine_index_from_value(), the
    retracted bin-division model, and so mislabelled every preset stored at
    indices 13..16 by one position -- `Vocoder` came back as `Bass`, `Bass`
    as `SawX`, `SawX` as `Harm`, `Harm` as `WaveUser`. The measured table had
    replaced the model in set_osc_engine() but not here, so writes and reads
    disagreed. Caught by a round-trip assertion while building the per-engine
    survey: set_osc_engine(rows, "Vocoder") read back as "Bass".
    """
    block = _tlv_records(rows)
    records, _, idx = _find_field_record(block, *FIELD_TLV["osc_type"])
    body = records[idx].body
    return engine_from_value(((body[2] & 0x7F) << 8) | body[1])


def set_osc_engine(rows, name):
    """Sets the oscillator engine by name, the way the device encodes it."""
    if name not in ENGINE_VALUES:
        raise ValueError("unknown engine %r" % name)
    value = ENGINE_VALUES[name]
    from microfreak.preset.tlv import serialize
    from microfreak.sample.encode import pack_7to8
    block = _tlv_records(rows)
    records, end, idx = _find_field_record(block, *FIELD_TLV["osc_type"])
    body = bytearray(records[idx].body)
    body[0] = ENGINE_COUNT_BYTE
    body[1] = value & 0xFF
    body[2] = (body[2] & 0x80) | ((value >> 8) & 0x7F)
    records[idx].body = bytes(body)
    rebuilt = serialize(records) + block[end:]
    if len(rebuilt) != len(block):
        raise ValueError("rebuild changed block length")
    rows[:] = rows_from_payload(pack_7to8(rebuilt))


# --- CONTROL: continuous 0-100 (or -100..100 for a few bipolar ones handled
# via mod-matrix-style sign, not present here -- these all use raw_to_display
# unsigned like State.controlValue) -------------------------------------
CONTROL = {
    "glide":               {"MSB": (6, 23),  "LSB": (6, 22),  "msb": (6, 16, 0x20)},
    "osc_type":            {"MSB": None,     "LSB": (0, 14),  "msb": None,
                             "mapping": _osc_type, "inverse": _OSC_TYPE_VALUES},
    "osc_wave":             {"MSB": (0, 27),  "LSB": (0, 26),  "msb": (0, 24, 0x10)},
    "osc_timbre":           {"MSB": (1, 7),   "LSB": (1, 6),   "msb": (1, 0, 0x02)},
    "osc_shape":            {"MSB": (1, 20),  "LSB": (1, 19),  "msb": (1, 16, 0x04)},
    "filter_cutoff":        {"MSB": (2, 30),  "LSB": (2, 29),  "msb": (2, 24, 0x10)},
    "filter_resonance":     {"MSB": (3, 9),   "LSB": (3, 7),   "msb": (3, 0, 0x40)},
    "cycling_env_rise":     {"MSB": (4, 6),   "LSB": (4, 5),   "msb": (4, 0, 0x10)},
    "cycling_env_fall":     {"MSB": (5, 2),   "LSB": (5, 1),   "msb": (5, 0, 0x01)},
    "cycling_env_hold":     {"MSB": (5, 12),  "LSB": (5, 11),  "msb": (5, 8, 0x04)},
    "cycling_env_amount":   {"MSB": (6, 6),   "LSB": (6, 5),   "msb": (6, 0, 0x10)},
    "cycling_env_rise_shape": {"MSB": (4, 20), "LSB": (4, 19), "msb": (4, 16, 0x04)},
    "cycling_env_fall_shape": {"MSB": (5, 26), "LSB": (5, 25), "msb": (5, 24, 0x01)},
    "arp_seq_rate_free":    {"MSB": (10, 5),  "LSB": (10, 4),  "msb": (10, 0, 0x08)},
    "arp_seq_rate_sync":    {"MSB": (9, 27),  "LSB": (9, 26),  "msb": (9, 24, 0x02)},
    "arp_seq_swing":        {"MSB": (10, 17), "LSB": (10, 15), "msb": (19, 8, 0x40)},
    "lfo_rate_free":        {"MSB": (13, 10), "LSB": (13, 9),  "msb": (13, 8, 0x01)},
    "lfo_rate_sync":        {"MSB": (12, 31), "LSB": (12, 30), "msb": (12, 24, 0x20)},
    "envelope_attack":      {"MSB": (14, 29), "LSB": (14, 28), "msb": (14, 24, 0x08)},
    "envelope_decay":       {"MSB": (15, 10), "LSB": (15, 9),  "msb": (15, 8, 0x01)},
    "envelope_sustain":     {"MSB": (15, 23), "LSB": (15, 22), "msb": (15, 16, 0x20)},
    "spice":                {"MSB": (0, 0),   "LSB": (0, 0),   "msb": (0, 0, 0x01)},
}

# --- SWITCH: enumerated named values ------------------------------------
SWITCH = {
    "filter_type": {
        "MSB": (2, 18), "LSB": (2, 17), "msb": (2, 16, 0x01),
        "values": [{"name": "LPF", "value": 0}, {"name": "BPF", "value": 0x4000},
                   {"name": "HPF", "value": 0x7FFF}],
    },
    "amp_mod": {
        "MSB": (14, 17), "LSB": (14, 15), "msb": (14, 8, 0x40),
        "values": [{"name": "Off", "value": 0}, {"name": "On", "value": 0x7FFF}],
    },
    "cycling_env_mode": {
        "MSB": (3, 25), "LSB": (3, 23), "msb": (3, 16, 0x40),
        "values": [{"name": "Env", "value": 0}, {"name": "Run", "value": 0x4000},
                   {"name": "Loop", "value": 0x7FFF}],
    },
    "lfo_shape": {
        "MSB": (12, 22), "LSB": (12, 21), "msb": (12, 16, 0x10),
        "values": [{"name": "Sine", "value": 0}, {"name": "Tri", "value": 0x1999},
                   {"name": "Saw", "value": 0x3333}, {"name": "Sqa", "value": 0x4CCC},
                   {"name": "SnH", "value": 0x6666}, {"name": "SnHF", "value": 0x7FFF}],
    },
    "arp": {
        "MSB": (9, 6), "LSB": (9, 5), "msb": (9, 0, 0x10),
        "values": [{"name": "Off", "value": 0}, {"name": "On", "value": 0x7FFF}],
    },
    "seq": {
        "MSB": (12, 5), "LSB": (12, 4), "msb": (12, 0, 0x08),
        "values": [{"name": "Off", "value": 0}, {"name": "On", "value": 0x7FFF}],
    },
    "arp_seq_mod": {
        # "1" was 17408, which put the list out of ascending order and so
        # broke switch_name's `raw <= entry.value` scan -- raw 5000 would
        # have decoded as "1" instead of "2". INFERRED, NOT MEASURED: every
        # other table here is evenly spaced across 0..0x7FFF and starts at
        # 0, including the 3-, 6- and 7-entry ones, and this table's other
        # three values (10922, 21845, 32767) are already exactly the 4-entry
        # grid. 0 is the only value consistent with all of that. Worth one
        # hardware check if the Arp/Seq mod decode is ever relied on.
        "MSB": (9, 18), "LSB": (9, 17), "msb": (9, 16, 0x01),
        "values": [{"name": "1", "value": 0}, {"name": "2", "value": 10922},
                   {"name": "3", "value": 21845}, {"name": "4", "value": 0x7FFF}],
    },
    "arp_seq_sync": {
        "MSB": (10, 27), "LSB": (10, 26), "msb": (10, 24, 0x02),
        "values": [{"name": "Off", "value": 0}, {"name": "On", "value": 0x7FFF}],
    },
    "lfo_sync": {
        "MSB": (13, 20), "LSB": (13, 19), "msb": (13, 16, 0x04),
        "values": [{"name": "Off", "value": 0}, {"name": "On", "value": 0x7FFF}],
    },
    "paraphonic": {
        "MSB": (16, 23), "LSB": (16, 22), "msb": (16, 16, 0x20),
        "values": [{"name": "Off", "value": 0}, {"name": "On", "value": 0x7FFF}],
    },
    "octave": {
        "MSB": (7, 4), "LSB": (7, 3), "msb": (7, 0, 0x04),
        "values": [{"name": "-3", "value": 0}, {"name": "-2", "value": 0x1555},
                   {"name": "-1", "value": 0x2AAA}, {"name": "0", "value": 0x4000},
                   {"name": "+1", "value": 0x5555}, {"name": "+2", "value": 0x6AAA},
                   {"name": "+3", "value": 0x7FFF}],
    },
}

# --- Mod matrix --------------------------------------------------------
#
# Cells live in the TLV stream as modules Co1..Co7 (destinations) each
# carrying five source fields, and are addressed by NAME rather than by byte
# position -- which is what makes this layout-independent. Earlier fixed-byte
# tables agreed with the hardware on only 59 of 390 presets, because the
# device reflows a preset's internal layout when it resaves.
#
MOD_MATRIX_FULL_SCALE = 2 ** 23   # display = int24 * 100 / MOD_MATRIX_FULL_SCALE


def mod_matrix_bytes_to_display(b0, b1, b2):
    """(byte0, byte1, byte2) -> display value, -100.0 .. +100.0."""
    n = b0 | (b1 << 8) | (b2 << 16)
    if n >= 0x800000:
        n -= 0x1000000
    return round(n * 100 / MOD_MATRIX_FULL_SCALE, 1)


def mod_matrix_display_to_bytes(display, byte0_zero=True):
    """display -> (byte0, byte1, byte2), inverse of the above.

    byte0 is held at 0 by default because the device never uses it: across all
    13650 mod-matrix cells in captures/mcc-presets.jsonl, byte0 is 0 every
    time. Quantising to a multiple of 256 costs 0.003 of display resolution,
    far below the 0.1 display step, so every 0.1 value still round-trips.

    (Note also that byte1 is NOT restricted to multiples of 32 -- real presets
    use 1, 2, 3, 4, 7, 9, 14 and so on. The old encoder assumed otherwise.)
    """
    n = int(round(display * MOD_MATRIX_FULL_SCALE / 100))
    if byte0_zero:
        # clamp to the largest in-range multiple of 256, so the extremes keep
        # byte0 = 0 too. +100.0 needs 2**23, one past the signed 24-bit max.
        n = int(round(n / 256.0)) * 256
        n = max(-0x800000, min(0x7FFF00, n))
    else:
        n = max(-0x800000, min(0x7FFFFF, n))
    n &= 0xFFFFFF
    return n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF


def encode_mod_matrix_display(display):
    """Kept for API continuity -- an alias for mod_matrix_display_to_bytes.

    The old implementation searched byte1/byte2 combinations against the
    per-byte scales and refused KNOWN_BAD_MOD_MATRIX_BYTES. Both are obsolete:
    those pairs were the visible corner of the formula error, not a device
    quirk, and the unified model needs no search.
    """
    return mod_matrix_display_to_bytes(display)



# (byte1, byte2) pairs confirmed live to decode one display-step (0.1) lower
# than the formula above predicts -- found by testing byte2=2 against every
# possible byte1 (0, 32, ..., 224): the two largest byte1 values (192, 224)
# both misbehave specifically when byte2=2; the other six don't, and byte1
# =192/224 combined with any other byte2 don't either. No root cause found
# (bit-collision and mod-matrix-assign-routing theories were both checked
# and ruled out -- see docs/PRESET_FORMAT.md) -- just avoid constructing these
# exact byte pairs. SUPERSEDED -- see the unified model above.
# Superseded: these were not a device quirk but the two visible cases of
# the per-byte formula error, now fixed. Kept only so old references resolve.
KNOWN_BAD_MOD_MATRIX_BYTES = {(192, 2), (224, 2)}


# Co1..Co7 are destinations; each carries five source fields. Addressing the
# cells by NAME through the TLV stream works on any preset layout, unlike
# fixed byte positions -- see microfreak-emu/docs/FORMATS.md.
_MOD_DESTS = ["pitch", "wave", "timbre", "cutoff", "assign1", "assign2", "assign3"]
_MOD_SOURCE_TAG = {"cyc_env": "EG1", "env": "EG2", "lfo": "LFO",
                   "press": "Xpr", "key_arp": "Key"}


def _mod_cell_record(block, source, dest):
    """-> (records, tlv_end, index of the cell's value record)."""
    from microfreak.preset.tlv import parse, T_MODULE, T_NAME, T_VALUE
    want_module = "Co%d" % (_MOD_DESTS.index(dest) + 1)
    want_field = _MOD_SOURCE_TAG[source]
    records, end = parse(block)
    module = pending = None
    for i, r in enumerate(records):
        if r.type == T_MODULE and r.length:
            module = r.text()
        elif r.type == T_NAME and r.length:
            pending = r.text()
        elif r.type == T_VALUE and pending is not None:
            if module == want_module and pending == want_field:
                return records, end, i
            pending = None
    raise KeyError("no mod-matrix cell %s -> %s in this preset" % (source, dest))


def get_mod_matrix_raw(rows, source, dest):
    """Returns the display value for one mod-matrix cell, -100.0..+100.0.

    Addressed by name through the TLV stream and decoded with the unified
    24-bit model (display = int24 * 100 / 2**23). Both were confirmed against
    the hardware display on 2026-09-04: the old fixed byte positions
    are only valid for presets in one particular layout, and the old per-byte
    scales were an approximation that went one step wrong on 86 byte
    combinations. See microfreak-emu/docs/FORMATS.md.
    """
    block = _tlv_records(rows)
    records, _, idx = _mod_cell_record(block, source, dest)
    return mod_matrix_bytes_to_display(*records[idx].body)


def set_mod_matrix_raw(rows, source, dest, display):
    """Sets one mod-matrix cell to a display value in -100.0..+100.0."""
    from microfreak.preset.tlv import serialize
    block = _tlv_records(rows)
    records, end, idx = _mod_cell_record(block, source, dest)
    records[idx].body = bytes(mod_matrix_display_to_bytes(display))
    rebuilt = serialize(records) + block[end:]
    if len(rebuilt) != len(block):
        raise ValueError("rebuild changed block length %d -> %d"
                         % (len(block), len(rebuilt)))
    from microfreak.sample.encode import pack_7to8
    new_rows = rows_from_payload(pack_7to8(rebuilt))
    rows[:] = new_rows


def mod_matrix_display_to_raw(display):
    """Kept for API compatibility with the old naive scheme -- just
    returns the display value itself now, since there's no clean single
    "raw" integer under the real model. Prefer set_mod_matrix_raw(rows,
    source, dest, display) directly."""
    return display


def mod_matrix_raw_to_display(raw):
    """See mod_matrix_display_to_raw -- raw IS the display value now."""
    return raw


MOD_SOURCES = ["cyc_env", "env", "lfo", "press", "key_arp"]
MOD_DESTINATIONS = ["pitch", "wave", "timbre", "cutoff", "assign1", "assign2", "assign3"]


# --- Assign destinations (Mat/Assign1..3) --------------------------------
#
# Measured 2026-09-10. The value is a pointer to a parameter:
#
#     value = (module_index << 8) | parameter_index
#
# `module_index` indexes MODULE_TABLE below (the firmware's own table at
# 0x08062478). `parameter_index` is 0-based into the module's *registration*
# order -- which is NOT the serialisation order, and includes parameters that
# are never written to a preset (VCO's ParaX1..3 and Pitch, EG2's Amount).
# That mismatch is why byte-position tables never worked here.
#
# Recovered by scanning the firmware for pointers into the parameter-name
# string regions (0x08062470-0x08062BA0 and 0x08075A70-0x08076240); each
# registration pool lists its module's parameters in order.
#
# Hardware-confirmed on 8 points: the owner set Assign1 to Filter Cutoff
# (-> 0x0101), Filter Resonance (0x0102) and Osc Timbre (0x0003), and read
# indices 1..6 of VCO back off the panel as VcoPar1, VcoPaX1, VcoPar2,
# VcoPaX2, VcoPar3, VcoPaX3.
MODULE_TABLE = ["VCO", "VCF", "EG1", "Kbd", "Arp", "LFO", "EG2", "Gen", "Sys",
                "Mat", "Co1", "Co2", "Co3", "Co4", "Co5", "Co6", "Co7",
                "SyA", "Seq", "Voc"]

ASSIGN_PARAMS = {
    "VCO": ["Type", "Param1", "ParaX1", "Param2", "ParaX2", "Param3", "ParaX3",
            "BendRng", "Pitch", "SmpIdx"],
    "VCF": ["Type", "Cutoff", "Reso"],
    "EG1": ["Mode", "RiseLvl", "RiseSlp", "FallLvl", "Hold", "FallSlp", "Amount"],
    "Kbd": ["Glide", "Octave", "Veloc", "Hold", "GlMode", "PresAmp", "Scale", "Root"],
    "Arp": ["Enable", "Range", "Div", "Rate", "Swing", "Sync", "Spice", "Dice",
            "Dir", "SeqOn"],
    # LFO's Sync shares a literal-pool entry with Arp's, so its exact slot
    # (3 or 4) is inferred from the serialisation order, not observed.
    "LFO": ["Shape", "Div", "Rate", "Sync", "Retrig"],
    "EG2": ["Mode", "Attack", "DecRel", "Sustain", "Legato", "Amount", "Snap"],
    "Gen": ["Parafon", "Panel", "PolyCnt", "PrstVol", "Volume", "UniSprd",
            "UniCnt", "Chord", "ChOffs1", "ChOffs2", "ChOffs3", "SmpHash"],
    "Sys": ["PrsetID", "PrsetBt", "Save", "Utility", "Shift", "ShfLED"],
    "Mat": ["MatEnc", "MatBtn", "Assign1", "Assign2", "Assign3", "KyMdSrc"],
    "Seq": ["Length", "XiceRst", "GateLen", "Smooth1", "Smooth2", "Smooth3",
            "Smooth4"],
    "Voc": ["HissMod", "HissVol"],
}
for _co in ("Co1", "Co2", "Co3", "Co4", "Co5", "Co6", "Co7"):
    ASSIGN_PARAMS[_co] = ["EG1", "EG2", "LFO", "Xpr", "Key", "Dest"]


ASSIGN_SLOTS = ("Assign1", "Assign2", "Assign3")

# Assign targets that must never be sampled. Modulating `Sys/Save` or the
# preset index could drive a flash write or a preset change on real hardware,
# and `MatEnc`/`MatBtn` are the front-panel encoder and button rather than
# anything audible. The factory bank never points an assign slot at module 8
# (`Sys`) either. `Mat/Assign1-3` are deliberately NOT fenced: 154 of 384
# factory presets use one assign slot to modulate another.
ASSIGN_FENCED = tuple(("Sys", p) for p in ASSIGN_PARAMS["Sys"]) + (
    ("Mat", "MatEnc"), ("Mat", "MatBtn"))


def assign_targets(exclude=()):
    """Every (module, parameter) an assign slot may point at.

    Excludes ASSIGN_FENCED, plus anything in `exclude` -- which the generator
    uses to keep a slot from pointing at itself, a case the firmware's display
    path special-cases and whose modulation behaviour is undefined.
    """
    fenced = set(ASSIGN_FENCED) | set(exclude)
    return [(m, p) for m in MODULE_TABLE
            for p in ASSIGN_PARAMS.get(m, ())
            if (m, p) not in fenced]


def get_assign(rows, slot):
    """-> (module, parameter) that `Mat/<slot>` points at, or None.

    The value is a 15-bit field in body bytes 1-2 like every other parameter,
    and decodes as `(module << 8) | parameter`. Body byte 0 is 0 in all 1,152
    factory assign records and is ignored by the firmware's own parser, which
    reads `*(ushort *)(value + 1)` -- see microfreak-emu/docs/FORMATS.md.

    Reading bytes 0-1 instead is self-consistent over the whole factory bank
    (body[0] is always 0 and body[1] never exceeds 6, inside VCO's 10
    parameters) and still wrong, so this is not a layout to re-derive by
    inspection. It was pinned by assigning Attack on the device and reading the
    slot back: body = 00 01 06 -> 0x0601 -> EG2/Attack.
    """
    block = _tlv_records(rows)
    records, _, idx = _find_field_record(block, "Mat", slot)
    body = records[idx].body
    return assign_decode(((body[2] & 0x7F) << 8) | body[1])


def set_assign(rows, slot, module, parameter):
    """Point `Mat/<slot>` at `module`/`parameter`. See get_assign for layout."""
    from microfreak.preset.tlv import serialize
    from microfreak.sample.encode import pack_7to8
    if parameter not in ASSIGN_PARAMS.get(module, ()):
        raise ValueError("%s has no parameter %r" % (module, parameter))
    block = _tlv_records(rows)
    records, end, idx = _find_field_record(block, "Mat", slot)
    body = bytearray(records[idx].body)
    body[0] = 0
    body[1] = ASSIGN_PARAMS[module].index(parameter)
    body[2] = (body[2] & 0x80) | (MODULE_TABLE.index(module) & 0x7F)
    records[idx].body = bytes(body)
    rebuilt = serialize(records) + block[end:]
    if len(rebuilt) != len(block):
        raise ValueError("rebuild changed block length %d -> %d"
                         % (len(block), len(rebuilt)))
    rows[:] = rows_from_payload(pack_7to8(rebuilt))


def assign_decode(value):
    """(module, parameter) named by a Mat/Assign* value, or None."""
    mi, pi = value >> 8, value & 0xFF
    if mi >= len(MODULE_TABLE):
        return None
    mod = MODULE_TABLE[mi]
    params = ASSIGN_PARAMS.get(mod)
    if not params or pi >= len(params):
        return None
    return mod, params[pi]


def assign_encode(module, parameter):
    """The Mat/Assign* value pointing at `module`/`parameter`."""
    if module not in MODULE_TABLE:
        raise ValueError("unknown module %r" % module)
    params = ASSIGN_PARAMS.get(module)
    if not params or parameter not in params:
        raise ValueError("%s has no assignable parameter %r" % (module, parameter))
    return (MODULE_TABLE.index(module) << 8) | params.index(parameter)


# --- Per-engine oscillator macro names ----------------------------------
#
# What the three oscillator knobs (Wave / Timbre / Shape) actually control,
# per engine. Read off the device panel 2026-09-10 by sweeping the Type knob;
# every engine name matched _OSC_TYPE_ENGINES, confirming that table's order
# end to end.
#
# This supersedes the partial list recovered from the firmware string table,
# which named only two parameters for most engines and mapped some of them to
# the wrong knob -- V. Analog in particular is Detune/Shape/Wave, not
# Waveshaper/Asym/Wave as the string order suggested.
#
# Where an entry is a discrete selector, VCO/Param1's range byte says how many
# positions (Wavetable/WaveUser 16 tables, Chords 11 types, Superwave 4 waves)
# -- see microfreak-emu/docs/FORMATS.md. Modulating those continuously lands between
# positions.
ENGINE_MACROS = {
    "Basic Waves":   ("Morph",      "Sym",       "Sub"),
    "Superwave":     ("Wave",       "Detune",    "Volume"),
    "Wavetable":     ("Table",      "Position",  "Chorus"),
    "Harmonic":      ("Content",    "Sculpting", "Chorus"),
    "KarplusStrong": ("Bow",        "Position",  "Decay"),
    "V. Analog":     ("Detune",     "Shape",     "Wave"),
    "Waveshaper":    ("Wave",       "Amount",    "Asym"),
    "Two Op. FM":    ("Ratio",      "Amount",    "Feedback"),
    "Formant":       ("Interval",   "Formant",   "Shape"),
    "Chords":        ("Type",       "Inv/Trsp",  "Waveform"),
    "Speech":        ("Type",       "Timbre",    "Word"),
    "Modal":         ("Inharm",     "Timbre",    "Decay"),
    "Noise":         ("Type",       "Rate",      "Balance"),
    "Bass":          ("Saturate",   "Fold",      "Noise"),
    "SawX":          ("SawMod",     "Shape",     "Noise"),
    "Harm":          ("Spread",     "Rectify",   "Noise"),
    "WaveUser":      ("Table",      "Position",  "Bitdepth"),
    # Sample's three macros shape playback; the sample itself is chosen with
    # Shift+Type on the panel and stored in Gen/SmpHash, not in a macro.
    "Sample":        ("Start",      "Length",    "Loop"),
    "Scan Grains":   ("Scan",       "Density",   "Chaos"),
    "Cloud Grains":  ("Start",      "Density",   "Chaos"),
    "Hit Grains":    ("Start",      "Density",   "Shape"),
    # Vocoder's Wave is hardcoded to saw on the device -- the knob is inert,
    # so modulating VCO/Param1 on this engine does nothing (owner, 2026-09-10).
    "Vocoder":       ("Wave (fixed: Saw)", "Shift", "Bandwidth"),
}


def engine_macro_names(engine):
    """(wave, timbre, shape) parameter names for an engine."""
    return ENGINE_MACROS[engine]


# --- Engine values: MEASURED, not derived --------------------------------
#
# The device's own VCO/Type value for each engine, obtained by saving one
# preset per engine from the Init on a 5.0.0.2084 device (2026-09-10). This
# replaces four successive fitted formulas, every one of which was wrong.
#
# Two facts these measurements establish that no fit could:
#
# 1. STORAGE ORDER != DISPLAY ORDER. Vocoder is stored at index 13, between
#    Noise and Bass, but appears LAST on the Type knob (display position 21).
#    Every fitted model assumed one list and broke on this.
# 2. Selection is HYSTERETIC. FUN_0805f458 biases the index by +-0.25 of a
#    step toward the currently selected engine, so a stored value near a
#    boundary resolves differently depending on what was selected before.
#    No pure function of the stored value is correct there -- which is why a
#    measured table is the right artefact, not a formula.
#
# Values are evenly spaced by 32768/22; the top one is clamped to the 15-bit
# maximum. ENGINE_ORDER is storage order. _OSC_TYPE_ENGINES remains DISPLAY
# order, which is what the panel shows and what users mean.
ENGINE_ORDER = [
    "Basic Waves", "Superwave", "Wavetable", "Harmonic", "KarplusStrong",
    "V. Analog", "Waveshaper", "Two Op. FM", "Formant", "Chords", "Speech",
    "Modal", "Noise", "Vocoder", "Bass", "SawX", "Harm", "WaveUser",
    "Sample", "Scan Grains", "Cloud Grains", "Hit Grains",
]

ENGINE_VALUES = {
    "Basic Waves":  1489, "Superwave":    2979, "Wavetable":    4468,
    "Harmonic":     5958, "KarplusStrong": 7447, "V. Analog":    8936,
    "Waveshaper":  10426, "Two Op. FM":  11915, "Formant":     13405,
    "Chords":      14894, "Speech":      16384, "Modal":       17873,
    "Noise":       19362, "Vocoder":     20852, "Bass":        22341,
    "SawX":        23831, "Harm":        25320, "WaveUser":    26809,
    "Sample":      28299, "Scan Grains": 29788, "Cloud Grains": 31278,
    "Hit Grains":  32767,
}
_ENGINE_BY_VALUE = sorted((v, k) for k, v in ENGINE_VALUES.items())


def engine_value(name):
    """The value the device itself writes for this engine. Exact."""
    if name not in ENGINE_VALUES:
        raise ValueError("unknown engine %r" % name)
    return ENGINE_VALUES[name]


def engine_from_value(value):
    """Best-effort engine name for an arbitrary stored value.

    APPROXIMATE by nature: nearest measured value. Selection is hysteretic,
    so a value near a boundary genuinely has no single correct answer -- the
    device's answer depends on what was selected before. Exact only for values
    the device itself wrote (i.e. those in ENGINE_VALUES).
    """
    return min(_ENGINE_BY_VALUE, key=lambda kv: abs(kv[0] - value))[1]


# --- Per-engine oscillator defaults --------------------------------------
#
# What the device writes into VCO/Param1..3 when you select an engine from the
# Init, measured 2026-09-10 (one save per engine). Useful as sane starting
# points for generated presets -- these are the values the instrument itself
# considers neutral for each engine.
#
# `range1` is VCO/Param1's range byte: 238 means continuous, anything smaller
# is a DISCRETE selector with range1 + 1 positions. Confirmed on the device
# for Superwave (4 waves) and Wavetable / WaveUser (16 tables); the corpus
# adds Chords (11 chord types). Modulating Param1 continuously on those
# engines lands between positions.
ENGINE_DEFAULTS = {
    # engine:        (param1, param2, param3, range1)
    "Superwave":     (    0, 16383, 16383,   3),
    "Wavetable":     (    0,  None,  None,  15),
    "WaveUser":      (    0,  None,  None,  15),
    "Harmonic":      (    0,  None,  8191, 238),
    "V. Analog":     (16319, 16319,  None, 238),
    "Waveshaper":    (16319, 16319,  None, 238),
    "Formant":       ( 8480, 28012, 26599, 238),
    "Modal":         ( 8223, 11564, 25699, 238),
    "Noise":         (10922, 32767, 16383, 238),
    "SawX":          (    0,  None,  None, 238),
    "Harm":          (    0,  None,  None, 238),
    "Sample":        (    0, 32767,  None, 238),
    "Scan Grains":   (24576, 16384,  None, 238),
    "Cloud Grains":  (    0, 16384,  None, 238),
    "Hit Grains":    (    0, 16384, 16384, 238),
    "Vocoder":       (    0,  None,  None, 238),   # Wave is hardcoded to saw
}


def engine_param1_positions(engine):
    """Discrete positions on VCO/Param1, or None if it is continuous."""
    e = ENGINE_DEFAULTS.get(engine)
    if not e or e[3] >= 238:
        return None
    return e[3] + 1

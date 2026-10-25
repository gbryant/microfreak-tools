> **SUPERSEDED IN PART (2026-09-04).** This document records how the format was
> worked out empirically. Several of its conclusions have since been corrected
> by firmware analysis and hardware tests — read **[FORMATS.md](https://github.com/gbryant/microfreak-emu/blob/main/docs/FORMATS.md)**
> first, and treat anything below that contradicts it as history:
>
> - The payload is a TLV stream, `header = (type << 5) | length`. `#` and `c`
>   are record headers, not delimiters, and the 3/5/8-byte "field widths" were
>   an artifact of measuring gaps between name anchors. Every value is 3 bytes.
> - `EPanel` is not a field name — the leading letter of every `FIELD_ANCHORS`
>   entry is a record header byte encoding the name's length.
> - The mod-matrix byte tables are layout-dependent (they matched the hardware
>   on only 59 of 390 presets) and the per-byte value formula was wrong;
>   `KNOWN_BAD_MOD_MATRIX_BYTES` was that error, not a device quirk.
> - `decode_preset.py` has been removed; `microfreak/preset/tlv.py` replaces it.

# Preset parameter format

Status: **construction-ready and hardware-verified**, 2026-09-03. The
transport encoding is solved. Our own name-anchor decode maps 41 fields
(fixed layout, no value semantics) but breaks on a conditional/polymorphic
region (`EPanel`) in most factory presets — no longer a blocker (see below).
[`RELATED_PROJECTS.md`](https://github.com/gbryant/microfreak-emu/blob/main/docs/RELATED_PROJECTS.md) surfaced an external field model (fixed `[chunk, byte-offset]`
addresses plus a value formula) covering essentially every named control,
switch, and the mod matrix. Its `CONTROL`/`SWITCH` portions validated
cleanly, both statically (`preset_fields_validate.py` against the full
399-preset corpus) and live on real hardware (`filter_cutoff`, `octave`,
`paraphonic` all wrote and read back exact matches; `osc_type`'s value table
was stale and got corrected from a live sweep). Its `MOD_MATRIX` portion,
however, turned out to be **wrong** on this hardware — writing through it
silently landed on bytes that decode back correctly by its own formula but
don't correspond to what the device actually reads. That's now replaced
with `MOD_MATRIX_WIRE`, a real hardware-derived table found via a
differential test with all 35 mod-matrix cells set to known, distinct
values, plus a from-scratch per-byte value formula (not prior art's) that
covers both positive and negative amounts. See "Mod matrix" below for the
full story — it also explains why `EPanel` is conditional. This is the
model `microfreak/preset/fields.py` and
`mfreak preset encode` build on. See [PROTOCOL.md](https://github.com/gbryant/microfreak-emu/blob/main/docs/PROTOCOL.md) for the surrounding
protocol context (addressing, how to fetch a preset's raw block) — this
document is only about what's inside that block.

## Confirmed

- **Preset data uses the same MIDI SysEx 7-to-8 packing as samples and
  wavetables** (see SAMPLE_ENCODING.md). A preset block is 146 chunks /
  4672 wire bytes (note: 146 chunks, not the 147 samples/wavetables use) →
  4672 / 8 = 584 groups exactly → 4088 real bytes after `decode_7to8()`.
  Confirmed both by inspection (decoded text reads as clean parameter names
  instead of fragments) and via `nervous.mfpz` (MCC's own single-preset
  local export — see below) matching the raw *wire* bytes (before 7-to-8
  decoding) byte-for-byte, 100%.
- The decoded payload is a real, self-describing parameter table, not an
  opaque blob: `#` marks the start of a module (`VCO`, `VCF`, `EG1`, `EG2`,
  `Kbd`, `Arp`, `LFO`, `Gen`, `Sys`, `Mat`, `Co1`-`Co7`, `Seq`), and each
  parameter is `<name text>` + `c` (0x63, a constant one-byte delimiter,
  no exception seen) + `<value bytes>`.
- **41 fields have a confirmed fixed byte width**, verified across 399 real
  captured presets (`VCODType` through `GenGParafon` — the full ordered
  list is `FIELD_ANCHORS` in `decode_preset.py`). Widths seen so far: 3
  bytes (the overwhelming majority), 5 bytes (`BendRng`, `Reso`, `Amount`,
  `PresAmp`, `SeqOn`, `Retrig`, `Legato`, `Volume`, `Utility`), 8 bytes
  (`Assign3`, and each connection's `Key` field in `Co1`-`Co7`). Not yet
  understood: what determines a field's width, or how to read a value's
  actual engineering meaning (e.g. real Hz for `Cutoff`) from its raw bytes.
- **The schema is genuinely conditional/polymorphic past `GenGParafon`**,
  not fully fixed. `EPanel` (a real field — confirmed via clean
  `<tag><name>c<3-byte-value>` structure at that position, not a
  coincidental text match) appears in only 38 of 399 captured presets.
  Systematically checked every one of the 41 confirmed-fixed fields'
  values as a potential predictor (partition all 399 presets by that
  field's value, check whether every partition is 100% either
  `EPanel`-present or `EPanel`-absent) — **none of them cleanly predicts
  it alone**, including the two leading candidates (`VCODType`, the
  oscillator engine selector, and `GenGParafon`, likely a paraphonic-mode
  toggle given the name and its exactly-two observed values). Either the
  trigger is a combination of fields, or it depends on something not
  checked yet.

## Value semantics: `CONTROL`/`SWITCH` solved and hardware-verified; mod matrix solved separately

`microfreak/preset/fields.py` ports `francoisgeorgy/microfreak-reverse`'s field tables
([`RELATED_PROJECTS.md`](https://github.com/gbryant/microfreak-emu/blob/main/docs/RELATED_PROJECTS.md)): `CONTROL` (continuous 0–100 params — oscillator
wave/timbre/shape, filter cutoff/resonance, cycling env rise/fall/hold/
amount, arp/seq/LFO rate, envelope attack/decay/sustain, glide, spice) and
`SWITCH` (enumerated — filter type, amp mod, cycling env mode, LFO shape,
arp/seq/LFO sync, paraphonic, octave). Addressing is `[chunk, byte_offset]`
directly on the raw (un-decoded) 32-byte transport chunks — mathematically
equivalent to our own 7-to-8 decode, just addressed per-field instead of
globally. Values reconstruct via `multibytes_value()`: a 15-bit value split
across an MSB byte, an LSB byte, and one bit borrowed from a nearby 7-to-8
header byte; `raw_to_display()`/`display_to_raw()` convert to/from the
0.0–100.0 value MCC would show.

**Validated two ways.** Statically, against the full 399-preset corpus
(`preset_fields_validate.py captures/mcc-presets.jsonl`): every field
decodes to a plausible, in-range value for all 391 standard-length presets,
including the 353 `EPanel`-affected ones — this model's addressing is
unaffected by whatever makes `EPanel` conditional. Cross-checked against our
own anchor-based byte ranges for `filter_cutoff`/`paraphonic` — exact
agreement. Then, live on real hardware (2026-09-03): wrote and read back
`filter_cutoff`, `octave`, and `paraphonic` — all matched the device display
exactly.

> **SUPERSEDED 2026-09-10.** The engine *names and their order* below are
> right -- they came from a physical knob sweep. The **value formula** is
> wrong twice over: `osc_type` is a 15-bit pair, not a 0..127 raw byte, and
> the device decodes it against its own engine count, not the saving
> firmware's. See [FORMATS.md](https://github.com/gbryant/microfreak-emu/blob/main/docs/FORMATS.md), "Oscillator engine encoding", which
> has nine panel-confirmed points behind it.

**One real bug found and fixed live**: `osc_type`'s name table was stale —
prior art's firmware had 12 oscillator engines, this hardware has 22
(`Basic Waves` through `Vocoder`; see `microfreak/preset/fields.py`'s
`_OSC_TYPE_ENGINES`). Found by writing raw byte 64 expecting "V. Analog" and
getting "Speech" on the real display; fixed by having the user sweep the
physical OSC TYPE knob and list all 22 names in order, then confirming the
new table's formula (`round((i+1)*127/22)`, same even-spacing pattern prior
art's 12-engine table used) against that single confirmed point. Only that
one raw value (64→Speech) is hardware-confirmed directly; the rest are
formula-interpolated. Re-verified live afterward: correct.

### Mod matrix: prior art's table is wrong on this hardware — solved independently

Writing through prior art's `MOD_MATRIX` table (kept as `MOD_MATRIX_STALE`
in `microfreak/preset/fields.py` for reference) produced bytes that decoded back to the
intended value by its own formula, but **the device never lit the
corresponding mod-matrix LED** — the write silently landed on the wrong
bytes. Root cause, found via a live differential test: **a preset's
internal layout is not fixed.** Activating a previously-zero mod-matrix
connection through the device's own UI causes it to resave the whole block
in a different arrangement — a single new connection produced a ~1126-byte
reflow (out of 4672) when diffed against the pre-edit version. Prior art's
addresses are only valid for a preset still in the original/"compact"
layout; a preset with any real, hardware-set mod-matrix connection already
saved into it is in a different, "expanded" layout.

The byte *positions* for the expanded layout are `MOD_MATRIX_WIRE` in
`microfreak/preset/fields.py`, found this way: the user set all 35 source/destination
cells to a distinct known value (0.1, 0.2, ... 3.5, entered row-major — all
7 destinations for Cyc Env, then Env, then LFO, Press, Key/Arp — confirmed
to match the physical panel's silkscreened row/column order) and saved.
Decoding that read-back showed the mod-matrix section is organized as seven
`Co<N>` blocks (`N`=1..7, in destination order pitch/wave/timbre/
cutoff/assign1-3) — literally the `Co1`-`Co7` connection groups
`decode_preset.py`'s `FIELD_ANCHORS` already named but never decoded — each
containing five `<tag>c<3 bytes>` fields in source order (`CEG1c`=cyc_env,
`CEG2c`=env, `CLFOc`=lfo, `CXprc`=press, `CKeyc`=key_arp).

**The 3-byte value is NOT a plain 24-bit little-endian integer** — that was
the first (wrong) guess, disproven by writing constructed byte patterns and
reading the real display back. Each byte has its own independent scale,
nailed down via isolated single-byte live writes (holding the other bytes
at 0) and cross-validated against all 35 real cells from the calibration
sweep:

- `byte1` is always a multiple of 32 in every observed real sample (8
  possible values, 0–224) and contributes `byte1/320` to the display value
  — exact, no rounding, confirmed across its full range.
- `byte2` contributes `byte2 * 0.78125` (= `byte2 * 100/128` — strongly
  suggesting its native range is 0–127, scaled straight to ~0–100, like a
  MIDI CC) to the display value, confirmed exactly for `byte2` = 1 through
  10 in isolation, including an exact-tie rounding case (`byte2=8`,
  landing exactly on a 0.05 boundary, resolved by standard round-half-up).
- `byte0` is untested (every real sample and every test here used 0) but
  presumed to extend the same pattern at `/81920`.
- Combine as `byte0/81920 + byte1/320 + byte2*0.78125`, **then round the
  sum to 1 decimal place once** (not per-component) — confirmed against
  34 of the 35 real cells exactly.

This is what `get_mod_matrix_raw()` computes and what
`encode_mod_matrix_display()` inverts (search over `byte2`, picking the
nearest achievable `byte1` for the remainder).

**One narrow, unexplained exception, found twice independently**: the pair
`(byte1=224, byte2=2)` decodes to one display-step (0.1) lower than the
formula predicts on the real device (confirmed via the real `press→pitch`
cell from the calibration sweep, and reproduced by our own write to an
unrelated cell, `env→wave`). A second, matching case was found the same
way: `(byte1=192, byte2=2)` also reads one step low (`cyc_env→assign1`,
live test). All *other* byte1 values (0, 32, 64, 96, 128, 160) combined
with `byte2=2` are exact, and `byte1=192`/`224` combined with any *other*
`byte2` are exact — so it's specifically these two byte pairs, not a
general rule about large `byte1` or about `byte2=2`. Checked and ruled out:
bit-collision in the shared transport-group header (the two failures don't
share a bit pattern that the many passing cases don't also share), and
mod-matrix-assign-slot routing (the first failure was the fixed `pitch`
destination, which has no assign routing at all). No root cause found.
**`KNOWN_BAD_MOD_MATRIX_BYTES` in `microfreak/preset/fields.py` lists both pairs, and
`encode_mod_matrix_display()` never selects them** — it nudges to the
adjacent `byte1` step instead, which occasionally means a target display
value isn't exactly reachable (falls back to the nearest achievable
value, confirmed within 0.1 across a 0.0–10.0 sweep with only 6 such
gaps).

**Negative values: solved.** Live differential test (set `cyc_env→pitch` to
+1.0 via the hardware UI, save, read; then to -1.0, save, read again; diff
the two): exactly 2 bytes changed, and they're precisely what a **standard
24-bit two's complement negation of the whole 3-byte value** produces —
`+1.0` is `(0, 64, 1)`; `0x1000000 - (0 | 64<<8 | 1<<16) == (0 | 192<<8 |
254<<16)`, matching the observed `-1.0` bytes `(0, 192, 254)` exactly. So:
treat `(byte0 | byte1<<8 | byte2<<16)` as a signed 24-bit integer — if
`>= 0x800000`, negate it back to the positive-magnitude bytes, apply the
same per-byte formula, then negate the display result. `get_mod_matrix_raw`
does this automatically; `encode_mod_matrix_display`/`set_mod_matrix_raw`
accept negative `display` directly. Re-verified live with a second,
independent write: `env→pitch = -2.5` and `lfo→wave = -3.5` both read back
exactly right (this is also the same specific value, `-2.5`, that
previously read as `-2.4` under the old wrong 24-bit-LE-of-the-*positive*-
formula model — now exact).

**Construction constraint this creates**: `mfreak preset encode` refuses `mod:*`
assignments against a template that isn't already in the expanded layout
(checked by searching for the `CEG1c` tag) rather than silently writing to
the wrong bytes. In practice this means: to build a preset with mod-matrix
connections, start from a template that already has at least one real
connection saved into it via the device's own UI — not an arbitrary
factory/user preset.

**New minor finding from validation:** 8 of the 399 captured presets are
not the standard 4672-byte (146-chunk) size at all (lengths from 2432 to
7488 bytes seen). Excluded from the corpus-wide checks above; not yet
investigated — rare enough to not block construction work, but worth
keeping in mind if a generated/round-tripped preset ever comes back an
unexpected length. Possibly related to the same layout-reflow phenomenon
that broke the mod-matrix table (more/bigger active connections needing
more space?) — not confirmed.

## `nervous.mfpz` — a different kind of local export than `.mfsz`/`.mfwz`

MCC can export a single preset (Presets page) to a `.mfpz` file — same ZIP
+ Boost.Serialization-text-archive container as `.mfsz`/`.mfwz`
(SAMPLE_ENCODING.md), confirmed via the same magic-byte check. But **unlike
the audio formats, this does not hand over a separately-decoded/labeled
model** — the archive's one array is just the raw 4672 *wire* bytes
(pre-7-to-8-decode), confirmed identical byte-for-byte to what this
project's own capture already had for the same preset. Reasonable
explanation: audio needs local desktop-side processing (resampling,
encoding) so MCC keeps a decoded working copy; preset parameters are
edited live against the connected device, so MCC has no reason to keep a
separately-parsed local model — "export" is just a raw dump of whatever
bytes it last saw. **This dead-ends the known-plaintext shortcut that
cracked the audio formats — worth recording so it isn't re-tried.**

## Tooling

- `decode_preset.py` — loads a `captures/*.jsonl` file's preset data-select
  traffic, 7-to-8-decodes each preset block, and (for presets matching the
  confirmed 41-field prefix) extracts each field's raw value bytes by
  searching for its name-anchor text and taking the gap to the next anchor.
  `--widths` shows the value-width distribution per field across every
  preset in the capture; `--slot N` dumps one preset's extracted fields.
  Only gets through the 41 confirmed fields — bails out at the first
  schema divergence (`EPanel` for most presets). Also exposes
  `load_decoded_presets(path, return_raw=True)` for getting a capture's raw
  (un-decoded) per-preset payloads, used by `preset_fields_validate.py`.
- `microfreak/preset/fields.py` — the field model described above: `CONTROL`/`SWITCH`
  tables (prior art's addressing, hardware-verified) plus
  `get_field_raw`/`set_field_raw` and `raw_to_display`/`display_to_raw`;
  `MOD_MATRIX_WIRE` (our own hardware-derived addressing for the expanded
  layout) plus `get_mod_matrix_raw`/`set_mod_matrix_raw` and
  `mod_matrix_raw_to_display`/`mod_matrix_display_to_raw`;
  `MOD_MATRIX_STALE` (prior art's mod-matrix table, kept only for
  reference — do not use for writes). Operates on `rows` — a preset payload
  reshaped into 146 rows of 32 raw wire bytes
  (`rows_from_payload`/`payload_from_rows`), exactly the shape
  `mfreak preset read`'s `read_preset()` already returns.
- `preset_fields_validate.py` — the corpus-wide bounds/enum check and the
  structural cross-check against `decode_preset.py`, described above.
  `usage: preset_fields_validate.py captures/mcc-presets.jsonl`.
- `mfreak preset encode` — builds a new preset by patching a template's
  payload at specific fields (named parameter → byte patch), as an MCC
  `.mfpz` for `mfreak preset write --file`. See its own docstring.

## Known gaps (not blocking preset construction)

> **The mod-matrix section above is partly superseded (2026-09-04).** The
> source/destination mapping is confirmed, but `MOD_MATRIX_WIRE`'s byte
> positions are layout-dependent (full agreement on only 59 of 390 presets)
> and the per-byte value formula was an approximation. Cells are now addressed
> by name through the TLV stream and decoded as one 24-bit fixed-point value,
> `display = int24 * 100 / 2**23`, confirmed against the hardware display.
> `KNOWN_BAD_MOD_MATRIX_BYTES` was the formula error, not a device quirk, and
> the "expanded layout" requirement is gone. See [FORMATS.md](https://github.com/gbryant/microfreak-emu/blob/main/docs/FORMATS.md).

> **Several of these are resolved (2026-09-04) by firmware analysis -- see
> [FORMATS.md](https://github.com/gbryant/microfreak-emu/blob/main/docs/FORMATS.md).** In short: the payload is a TLV stream with
> `header = (type << 5) | length`; `#` and `c` are headers, not delimiters;
> the 3/5/8-byte "field widths" were an artifact of anchor-gap measurement
> (every value is 3 bytes); `EPanel` is the header byte `0x45` plus a field
> named `Panel`; and the name record's byte 20 high nibble is a schema-version
> marker (`1` = no `Voc` module, `3` = with `Voc`). `microfreak/preset/tlv.py` parses
> 397/398 of the corpus with no fixed field list.


- **`EPanel`'s trigger is still unknown**, but likely the same phenomenon as
  the mod-matrix layout reflow (both are "preset resaved via device UI with
  richer content ends up in a different internal shape"), and doesn't block
  `CONTROL`/`SWITCH` construction: that model works identically whether or
  not a preset has `EPanel`. It does gate mod-matrix construction
  indirectly, via the expanded-layout requirement above.
- ~~Negative mod-matrix values unsolved~~ — solved (24-bit two's
  complement of the whole 3-byte value, see above).
- **The exact trigger for the "expanded" mod-matrix layout isn't
  pinned down** — inferred to be "at least one real connection has been
  saved via the device UI," not tested against every possible edge case
  (e.g. does *any* nonzero connection trigger it, or only above some
  threshold?).
- **The 8/399 non-standard-length presets** found during validation —
  not investigated. `mfreak preset encode` only works with standard
  4672-byte templates; a non-standard one would need its own handling.
  Possibly related to the layout-reflow phenomenon (more/bigger active
  connections needing more space?) — not confirmed.
- **Fields outside `microfreak/preset/fields.py`'s coverage**: the `Sys`/`Utility`
  group and sequencer step/note/gate data have no known mapping at all
  (prior art doesn't cover them either).
- Our own `decode_preset.py`'s `FIELD_ANCHORS` past `GenGParafon` (`Arp`
  onward through `Seq`/`Smooth4`) were read by eye from decoded text
  ([PROTOCOL.md](https://github.com/gbryant/microfreak-emu/blob/main/docs/PROTOCOL.md)) but never anchor-verified the way the first 41
  fields were — low priority now that `microfreak/preset/fields.py` covers most of
  the same ground with actual value semantics.

## Files

- `captures/preset-format/nervous-exported.mfpz` / `nervous-exported-archive.txt`
  — MCC's single-preset local export for slot 0 ("NervousKeys") and its
  unzipped Boost archive. Confirmed to just contain the raw wire bytes
  (see above) — kept for reference so this dead end isn't re-investigated,
  and in case a *different* preset's export ever looks structurally
  different.
- `decode_preset.py`, `microfreak/preset/fields.py`, `preset_fields_validate.py`,
  `mfreak preset encode` — the tooling described above.

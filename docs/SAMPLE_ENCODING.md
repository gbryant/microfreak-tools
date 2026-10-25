# Sample audio encoding — SOLVED

Cracked via a known-plaintext attack: MCC's own `.mfsz`/`.mfwz` export files
turned out to be ZIP archives containing a human-readable
Boost.Serialization text array of the sample/wavetable's internal PCM data.
Comparing that directly against the real wire bytes MCC sent for the exact
same audio gave a 100% exact byte match once the transport encoding was
understood, and a from-scratch encoder built from that understanding was
verified live on real hardware — a freshly-recorded WAV, encoded from
nothing but the format described below, sounded correct by ear on the
device. See [PROTOCOL.md](https://github.com/gbryant/microfreak-emu/blob/main/docs/PROTOCOL.md) for the surrounding protocol context
(addressing, chunking, the read/write message sequence) — this document is
only about what the 32-byte chunk *payloads* mean.

## The encoding

1. **16-bit signed PCM, little-endian, 32kHz, mono.** Exactly matches the
   product's own quoted spec — the "26-bit" figure mentioned early in this
   investigation was indeed a mishearing/typo for 16-bit.
2. Audio is grouped into **blocks of 2048 samples (4096 bytes)**, each
   followed by **20 bytes of zero padding**, making 4116 "real" bytes per
   block.
3. Each 4116-byte block is packed with the **standard MIDI SysEx 7-to-8
   scheme**: every 7 real (signed 8-bit) bytes become 8 wire bytes — one
   header byte whose bit `i` holds bit 7 of the `(i+1)`-th real byte in the
   group, followed by the 7 real bytes with bit 7 masked off (0-127).
   `4116 / 7 = 588` groups exactly, `× 8 = 4704` wire bytes per block —
   exactly the fixed 147-chunk transport block size (`147 × 32 = 4704`)
   used everywhere else in this protocol (presets, wavetables, samples
   alike). This is almost certainly the *general* chunk-payload encoding
   for the whole `cmd 0x20` family, not something sample/wavetable-specific
   — worth checking against preset data too.

Reference implementation: `mfreak sample encode` (WAV → wire bytes, with a
built-in self-check that decodes its own output and compares against the
resampled input before writing anything) and `mfreak sample write` /
`mfreak wavetable write` already implement the send side.

## How it was found

1. **The "z" suffix was the tip-off.** `.mfsz` (samples) and `.mfwz`
   (wavetables) both end in "z" — checked the magic bytes of an exported
   `.mfsz` file: `50 4B 03 04`, a standard ZIP signature.
2. Unzipping gives one member (named after the sample/wavetable) starting
   `22 serialization::archive 10 0 ...` — the exact signature of C++
   **Boost.Serialization**'s text archive format. Plain ASCII, directly
   readable — a length-prefixed string (the name), some small header/flag
   fields, then a length-prefixed integer array: the sample count as a
   token, followed by exactly that many whitespace-separated signed
   integers (-128..127, true 8-bit signed — the device's *internal*
   representation, wider than the 7-bit-clamped wire format).
3. **Confirmed this is MCC's local sample-bank cache, not a device
   read-back** (told directly by the user after a red herring where the
   exported byte count coincidentally matched something written via raw
   SysEx, which briefly looked like evidence of an undiscovered read
   opcode — it wasn't; recorded here so it isn't re-chased).
4. With a `.mfwz` export of a wavetable this project already has fully
   solved, bidirectional, hardware-verified wire data for ("Convergence",
   via `mfreak wavetable read`/`mfreak wavetable write`), the internal array's
   values visibly came in pairs — one slowly-varying (a real waveform
   shape) and one noisy-looking (fine detail) — a strong visual tell for a
   16-bit sample split into low/high bytes.
5. Manually decoding the first wire frame by eye against the array's first
   few dozen values revealed the exact 7-to-8 packing pattern (values
   matched exactly once the appropriate header byte's bits were applied as
   the missing 8th bit of each subsequent byte).
6. Implemented `decode_7to8()`, ran it against the *entire* frame: 4096 of
   4116 decoded bytes matched the array exactly, with the remaining 20
   trailing bytes explained as zero-padding (confirmed present, identical,
   in every block of a much larger sample capture too, not just this one
   frame).
7. Applied the same decode to the sample case (`captures/send-1-sample-bank.pcapng`'s
   real wire bytes for a "bee buzz" recording) against its own `.mfsz`
   array, stripping the same per-block 20-byte padding: **100.00% exact
   match, 371,712 / 371,712 bytes.**
8. That match also resolved the sample-rate question cleanly: 371,712 bytes
   = 185,856 16-bit samples; the source WAV was 5.808s; 185,856 / 5.808 =
   **exactly 32,000 Hz**.
9. Built `mfreak sample encode` from this understanding (resample to 32kHz,
   16-bit, block+pad, 7-to-8 pack) and tested it against the *same* known
   WAV: output length matched MCC's real transfer exactly (428,064 bytes,
   91 blocks), and 43.5% of bytes matched MCC's own output exactly — not
   the ~0.4% you'd see from a wrong format, but not 100% either, because
   resampling from 24kHz to 32kHz isn't deterministic across
   implementations (this project's linear interpolation vs whatever MCC
   uses internally). The format match was already proven in step 7-8; this
   difference is expected and not a concern.
10. **Verified live on real hardware**, the actual test that matters:
    encoded the original "bee flying loop" WAV from scratch (no captured
    reference bytes involved) and wrote it to a slot with `mfreak sample write`.
    Confirmed by ear — sounds correct.
11. Built `mfreak wavetable encode` (same wire encoding, targeting the 4-frame/
    2048-samples-per-frame wavetable structure instead of a sample's
    chained blocks) and `mfreak wavetable write` to send its output. Verified live: wrote a from-scratch encoded wavetable to a
    slot, and unlike samples, wavetables have a real read-back
    (`mfreak wavetable read`), so this was confirmed **byte-exact** on
    read-after-write, not just by name.
12. **Cycle-level structure**, from public documentation of MCC's own
    import behavior (secondhand, not derived from a capture — but it
    reconciles exactly with the measured 8192-sample total, so treated as
    reliable): the 8192 samples aren't just "4 frames" at the transport
    level, they're **32 audio cycles of 256 samples each** (4 frames × 8
    cycles) — "frame" is how the wire protocol addresses transfers, "cycle"
    is what the oscillator actually steps through as the Timbre knob turns.
    MCC's own import logic: source cycle 0 → output cycle 0, source's last
    cycle → output cycle 31 always, with the cycles in between evenly
    spaced across the source's range and linearly crossfaded at fractional
    positions — the same algorithm whether the source has more or fewer
    than 32 cycles. Arbitrary audio with no known cycle structure (a
    recording, not a wavetable file) instead gets its time axis sliced into
    32 equal segments. `mfreak wavetable encode` implements both
    (`--cycle-samples N` for the former, omitted for the latter as the
    default).

## Notes for extending this

- **Presets**: confirmed yes — the same 7-to-8 packing applies to preset
  patch data too ([PROTOCOL.md](https://github.com/gbryant/microfreak-emu/blob/main/docs/PROTOCOL.md)). Decoding a real preset block
  reveals a genuine `<type-tag><name>c<value>` field structure, much
  cleaner than the raw (still-packed) reading originally documented there.
  Not decoded further than confirming the pattern exists — this is now the
  main open reverse-engineering task for the whole project, since cracking
  it is what would let new presets be authored from scratch rather than
  only read, cloned, or renamed.
- The 20-byte trailing pad per block is confirmed present and always zero
  in every case checked, but *why* — a fixed reserved region, alignment
  padding, something structural — isn't understood, just confirmed
  consistent.
- `mfreak wavetable encode`'s cycle-aware mode (`--cycle-samples`) hasn't been
  tested against a real multi-cycle wavetable source file (e.g. an actual
  Serum export) — only sanity-checked against synthetic data confirming the
  crossfade math behaves as intended (first/last cycles preserved exactly,
  intermediate positions correctly blended). Worth a real-file test and a
  live listening check if picked back up.

## Files

- `captures/sample-encoding/bee-flying-original.wav` — source WAV (stereo,
  16-bit, 24000 Hz, 5.808s).
- `captures/sample-encoding/bee-flying-wire.bin` — real wire bytes MCC sent
  for it (428,064 bytes, from `captures/send-1-sample-bank.pcapng`).
- `captures/sample-encoding/bee-buzz-exported.mfsz` /
  `bee-buzz-exported-archive.txt` — the `.mfsz` export and its unzipped
  Boost archive (the known-plaintext pair that cracked this).
- `captures/sample-encoding/convergence-frame0-wire.bin` /
  `convergence-allframes-wire.bin` — known-good wavetable wire bytes
  (from this project's own verified `mfreak wavetable read`), used to validate
  the encoding independent of the sample case.
- `captures/sample-encoding/convergence-exported.mfwz` /
  `convergence-exported-archive.txt` — the `.mfwz` export and its unzipped
  Boost archive for the same wavetable (16,384-token array = 8192 16-bit
  samples = 4 frames × 2048).
- `captures/sample-encoding/vinyl-wire.bin` — a second sample's wire bytes,
  no matching WAV/`.mfsz` pair.
- `mfreak sample encode` — WAV → MCC `.mfsz`, with a round-trip self-check
  through the wire encoding.
- `mfreak wavetable encode` — WAV → MCC `.mfwz`, slicing or cycle-aware, also
  self-checked. Pairs with `mfreak wavetable write --file`.
- `microfreak/mccfile.py` converts between the `.mfsz`/`.mfwz` PCM and the
  wire blocks. It reproduces MCC's own `.mfsz` and `.mfwz` archives byte for
  byte, and 'bee buzz' converts to exactly the 428,064 wire bytes MCC sent.

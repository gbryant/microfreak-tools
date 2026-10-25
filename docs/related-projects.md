# Related projects

Other public MicroFreak work, what each one adds, and how it relates to this
tool. Everything here targets firmware **5.0.0.2084**, and none of these
projects distributes Arturia's firmware — every one of them, including this
one, makes you bring your own copy.

If you maintain one of these and something below is wrong or out of date,
please open an issue.

## ThisFirmwareDoesNotExist / `freakpatch`

**[ThisFreakDoesntExist/ThisFirmwareDoesNotExist](https://github.com/ThisFreakDoesntExist/ThisFirmwareDoesNotExist)** (MIT).
Patches the main image in place, gated on the official file's SHA-256.

- **Separate Release.** The Dec/Rel knob becomes Decay only; Shift + Dec/Rel
  sets Release, which is saved in presets and responds to MIDI CC 72. Presets
  saved with it still load on stock firmware.
- **Mod matrix over MIDI CC.** Stock exposes exactly one matrix cell to MIDI
  (Filter Amt, CC 26). This gives CCs to the other 34 amounts, in and out.

Its `docs/` directory is the most complete public map of the MicroFreak's main
image: the memory map and MCU identification, the `.mff` container and its
XOR-8 checksum, the parameter and preset systems, the screen and LED paths, the
oscillator engine table, and a hard-won account of why the image cannot grow.
If you want to write your own patch, start there.

**It flashes through MIDI Control Center**, not over USB. That is the main
practical difference from this tool, which talks DFU to the instrument directly
(`mfreak fw flash`).

### Findings we can corroborate

Our work was derived independently — Renode whole-system emulation and hardware
runs, against their Ghidra and Unicorn — so where the two agree, the address is
about as well attested as it gets without Arturia's source:

| Finding | Status here |
|---|---|
| Boot splash string `Firmware version\n%d.%d.%d.%d` at `0x080757AC` | Same address. We patch it, and it has been **flashed and confirmed on hardware** — it is the boot splash, and 16 characters render. |
| Preset serializer at `0x08054A68` | Same function. `microfreak/preset/tlv.py` derives its framing from the two `bfi` instructions there: `header = (type << 5) \| length`. That explains the `0x23` and `0x63` bytes their notes carry as constants. |
| The 20 registered parameter groups | Their list comes from the flash strings at `0x08062478`. Decoding 22 presets read off our instrument gives exactly that list — nothing missing, nothing extra. |
| STM32F446, load base `0x0800C000`, initial SP `0x20020000`, default handler `0x0805F89C`, bootloader in the low 48 KB, no signature or MAC on the update file | All match. |

One thing the combination turns up that neither project had alone: the `Voc`
parameter group is **saved conditionally** — it appears in 5 of our 22 captured
presets. It does *not* track the oscillator engine (present on two Speech and
three Basic Waves presets, absent on two other Speech presets), so whatever
selects it is still unknown.

## FreakSwap

**[doriamxyz/FreakSwap](https://github.com/doriamxyz/FreakSwap)**
([web tool](https://doriamxyz.github.io/FreakSwap/)). A static browser tool that
replaces **one** stock oscillator engine with one of 11 engines adapted from
Mutable Instruments' Plaits and stmlib — 38 prebuilt engine-into-host-slot
combinations. Your firmware is read in the browser and never uploaded, and the
base file, the patch and the result are all hash-checked.

Vocoder is never used as a host slot. Speech **is** one of the seven engines
that can be replaced, so a FreakSwap build that overwrites Speech and this
tool's speech vocabulary patch are mutually exclusive by construction.

The engines carry Plaits' MIT notice, which is the same upstream this tool's LPC
tables come from — see [Provenance](../README.md#provenance).

## Freakier Firmware / SuperFreaker

**[freakierfirmware.com](https://freakierfirmware.com/)**. Browser-based
patchers for the MicroFreak (SuperFreaker) and the Roland SP-404MKII, in three
free packs: Essentials (separate release, legato glide, value readouts, MIDI
additions), Sound (drive, lo-fi, noise, tremolo, tape echo with reverb/reverse/
hold, wavefolder, sub-octave) and Loud (+7 dB headphones and line out). Free,
donation-funded, patching runs locally in the browser. We have not seen a source
repository.

## mf-utils

**[dcower/mf-utils](https://github.com/dcower/mf-utils)**. A Python toolkit from
the 3.0.x era that extracts the factory wavetables from an `.mff` to WAV and
replaces them. It was the earliest public documentation of the `.mff` container.
It does not load 4.x or 5.x files, and since 4.x the user wavetable slots can be
replaced through MIDI Control Center without any firmware change — or with
`mfreak wavetable write`.

## Mutable Instruments Plaits

**[pichenettes/eurorack](https://github.com/pichenettes/eurorack)** (`plaits/`,
`stmlib/`), Émilie Gillet's Eurorack firmware, MIT. Several MicroFreak
oscillator engines are adapted from it, and the Speech engine is its LPC speech
synthesiser: the frame struct, the phoneme table, the quantisation tables and
the 640-entry excitation pulse are byte-exact. The credit for the synthesiser
this tool's speech work builds on is Gillet's. Keep the notice with anything you
port.

## What this tool covers that the others don't

Not a boast — a map, so nobody re-derives what is already done:

- **Direct USB DFU flashing.** The Arturia vendor class requests (`0xF2` reboot
  to DFU, `0xF4` select target, `0xF3` finish) and the full download sequence,
  validated byte-for-byte against a capture of MIDI Control Center's own
  updater. `mfreak fw flash --plan` prints the exact request sequence without a device
  attached.
- **The two SysEx opcode dispatchers** and their jump tables, which is what
  makes it possible to claim an unused opcode.
- **LPC speech vocabulary.** Custom words in the Speech engine's banks, with an
  encoder that runs offline (`mfreak speech preview`) so you can hear what the engine
  will say before flashing anything.
- **Preset, sample and wavetable I/O over SysEx**, and the preset TLV format.

Deeper firmware notes, the Renode rehosting harness and the analysis behind all
of the above live in **microfreak-emu**.

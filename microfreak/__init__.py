"""microfreak-tools -- read, write and patch an Arturia MicroFreak.

Not affiliated with, authorised by or endorsed by Arturia. No Arturia code or
data is in this package; every command that needs a firmware image takes the
path to one you already own.

    microfreak.midi         the transport: port discovery, framing, replies
    microfreak.preset       presets: fields, TLV, read, write, select, encode
    microfreak.sample       sample encoding and upload
    microfreak.wavetable    wavetable encoding, read and upload
    microfreak.bank         .mfprojz / .mfsbz / .mfwbz bank files
    microfreak.lpc          the Speech engine's LPC bitstream: encode and decode
    microfreak.firmware     .mff containers, patching, and USB DFU flashing
    microfreak.plugin       plugins: custom firmware and the commands for it
    microfreak.cli          the `mfreak` entry point -- a dispatch table

Docstrings here cite the documents that record the evidence for a claim --
`docs/FORMATS.md` for the preset format, `docs/PROTOCOL.md` for the SysEx
protocol, `docs/SPEECH.md` for the LPC engine, `docs/FIRMWARE.md` for the
firmware map. Those live in the companion analysis repository,
**microfreak-emu**, together with the Renode harness the firmware work was
done in. They are cited rather than copied so a claim can be checked against
the run that produced it.

Presets are 1-512, samples 1-128, wavetables 1-16 -- the numbers the
instrument shows, which are what every command takes and prints. The
0-based indices the wire uses stay inside the package; `--json` carries
both, `slot` 0-based and `panel` 1-based. See `microfreak.panel`.
"""

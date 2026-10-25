# Driving `mfreak` from a program

This file is for an LLM or a script operating this tool for someone. It covers
what `--help` does not: which commands are dangerous, which facts are easy to
get wrong, and how to check your work.

Start with:

    mfreak capabilities

It returns JSON describing every command: whether it needs an instrument or a
firmware image, what it writes, its flags, and a one-line summary. It is
generated from the command table, so prefer it to anything written here.

## The JSON contract

`--json` works on every command.

* **stdout is exactly one JSON object.** Human-readable output goes to stderr.
* **Exit codes:** `0` success, `1` the operation failed, `2` bad usage.
* Every object carries `ok`, `command` and `exit`. On exit 1 it also carries
  `error`. On exit 2 it does not; the message is on stderr.
* Read the exit code first and the object second.

```
$ mfreak preset read 8 --json
{"ok": true, "command": "preset read", "exit": 0,
 "slot": 7, "panel": 8, "name": "08 Waveshaper", "bytes": 4672, "out": null}

$ mfreak preset decode /nope.mfpz --json ; echo $?
{"ok": false, "command": "preset decode", "exit": 1,
 "error": "/nope.mfpz: no such file"}
1
```

These commands report structured detail: `doctor`, `capabilities`,
`preset read`, `preset write`, `preset select`, `preset encode`,
`preset decode`, `sample encode`, `wavetable encode`, `asset info`,
`plugins`, `speech preview`, `speech analyze` and `peek dump-bootloader`.
The rest report only
`ok`, `command`, `exit` and `error`, so branch on the exit code.

## Hard rules

**Pass the number the instrument shows**: presets 1-512, samples 1-128,
wavetables 1-16. If the user says "preset 180", pass `180`. Applying an offset
writes to the neighbouring preset and destroys it.

`--json` reports both `panel` (the number shown) and `slot` (`panel - 1`, the
0-based index MIDI uses). **Read `panel`, and never pass `slot` back in.**

**Never run a command whose `writes` is `"flash"` without explicit human
confirmation in the current conversation.** Read that set from
`capabilities`; today it is exactly two:

* `fw flash` writes the instrument's flash, and can leave it unresponsive.
  By default it writes every image in the file. `fw flash FILE --plan`
  sends nothing and needs no instrument.
* `fw enter` writes no firmware, but clears image 0's validity marker and
  reboots into the bootloader. The instrument is unusable until
  power-cycled.

Recovery from either is holding SHIFT + ARP + OCT/MOD at power-on. Say so
before you suggest one.

**Treat `writes: "device"` as needing confirmation too.** It overwrites a
preset, sample or wavetable. It is reversible only if you read the slot first
and kept the file. Sample audio cannot be read back at all.

**`peek dump-bootloader` writes a file containing the instrument's serial
number.** Its JSON says so with `contains_serial` and `do_not_publish`. Never
paste that file, or any hex from it, into anything shared. `--redact-serial`
blanks the serial.

**Never commit or upload firmware, bootloader dumps or factory vocabulary.**
They belong to Arturia and the user. Both repositories' `.gitignore` refuse
`*.mff`, `*.raw`, `*.bin`, `*.syx` and `*.wav`.

## Working with no instrument

Eleven commands need nothing plugged in and no firmware image: `asset import`,
`asset info`, `capabilities`, `capture analyze`, `doctor`, `plugins`,
`preset convert`, `preset decode`, `preset encode`, `sample encode` and
`wavetable encode`, and with the speech plugin, `speech preview` and
`speech analyze`. That list is generated from `capabilities` as
`needs_nothing`, and `tests/test_cli_surface.py` checks this copy against it.

`mfreak doctor --offline --json` is the right first call on an unknown
machine. It reports `ready` and a `missing` list, and exits 0 when the offline
path works. **An unplugged instrument is not a failure.**

To exercise the device commands without hardware, use the emulator:

    mfemu serve --preset 7=Speech          # leave running, from microfreak-emu
    MFREAK_EMULATOR=1 mfreak preset read 7 --json

Run with `MFREAK_EMULATOR=1` from the **microfreak-emu** checkout, which has
both packages. From this checkout the import fails and the command says so.

## Plugins

`speech` and `peek` commands come from plugins, and exist only when the plugin
is installed. `mfreak plugins --json` lists what is installed, and
`capabilities` includes their commands. Each plugin also adds a patch,
`fw patch speech` or `fw patch peek`. A command from a plugin that is not
installed exits 2 and says how to install it. Do not install one without
asking.

## Two name traps

`capture analyze` reads a recorded SysEx/MIDI capture. `speech analyze` reads
audio. They are different commands.

`preset decode` takes either the 4,672-byte file `preset read --out` writes or
a 4,088-byte decoded block. It refuses anything else.

## Checking your own work

* `preset write`, `sample write` and `wavetable write` read the slot back
  unless `--no-verify`. Their JSON carries `verified`: `true`, `false`, or
  `null` when skipped. Treat `null` as unverified.
* **A failed read-back exits 1.** `config write` does the same, and lists the
  settings that failed in `failed`. So do `preset restore`, `preset erase`,
  `sample restore`, `sample erase`, `wavetable restore` and `wavetable erase`,
  which check each slot they change.
* Those commands also carry `written`: `false` when nothing was written (no `--yes`),
  `true` once sent. A dry run reports the same `slot` and `panel` the real
  write would use.
* **In JSON, `slot` is always 0-based and `panel` is always `slot + 1`**, in
  every command including `asset info` and `sample list`.
* `sample encode` and `wavetable encode` decode their own output and compare.
  A mismatch writes nothing and exits non-zero.
* `fw patch` refuses any image but stock firmware 5.0.0.2084. If it refuses,
  the image is wrong. Do not look for a way to force it.
* `tests/test_cli_surface.py` runs commands through `python -c "from
  microfreak.cli import main; ..."` rather than `-m microfreak.cli`. Keep it
  that way: under `-m`, some Python 3.14 patch releases print the wrong usage
  line and the test fails. The same applies to `mfemu()` in microfreak-emu.

## Where a claim comes from

The evidence for every format and protocol claim is in **microfreak-emu**,
under `docs/`: `FORMATS.md` for presets, `PROTOCOL.md` for SysEx, `SPEECH.md`
for the LPC engine, `FIRMWARE.md` for the firmware map, and `METHOD.md` for
retracted measurements. Cite those rather than inventing a justification. If a
document contradicts the code, the code is what ships; say that they disagree
instead of picking one silently.

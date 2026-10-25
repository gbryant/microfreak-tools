#!/usr/bin/env python3
"""Describe this tool's whole surface, for a program rather than a person.

    mfreak capabilities              one JSON object on stdout
    mfreak capabilities --text       the same thing, readable

An agent calls this once instead of scraping `--help` for every command. It
answers the questions a caller has to settle before it can act safely:

    which commands need an instrument plugged in,
    which need a firmware image the user supplies,
    which WRITE something, and what they write,
    what flags each one takes,
    and which commands can be run with nothing at all.

**The command list cannot drift from the code.** It is generated from
`cli.ROUTES` and the installed plugins, and the flags are read out of each
command's own argparse parser at run time. The one hand-written part is the classification below -- whether a
command needs a device and what it writes, which no amount of introspection
can tell you -- and `_check()` fails loudly if a command is added to ROUTES
without being classified here, or classified here without existing.

Every entry's `writes` field is the thing to branch on:

    null        reads or computes only; safe to call speculatively
    "file"      writes a file you named; safe for the instrument
    "device"    changes the instrument's state -- presets, samples, selection
    "flash"     writes the instrument's FLASH and can leave it unresponsive

A caller that does not have explicit permission from a human should refuse
`"flash"` outright, and should treat `"device"` as needing confirmation.
"""
import argparse
import contextlib
import importlib
import io
import json
import re
import sys

from microfreak import jsonout
from microfreak.cli import ROUTES, __version__, routes

# command path -> what no introspection can tell you.
#   device:   does it need an instrument on a MIDI port?
#   image:    does it need a firmware image the user supplies?
#   writes:   null | "file" | "device" | "flash"
#   summary:  one line, imperative.
META = {
    ("doctor",): dict(
        device=False, image=False, writes=None,
        summary="Report what works on this machine and the fix for what "
                "does not. Exits 0 when the offline path works; an "
                "unplugged instrument is not a failure."),
    ("capabilities",): dict(
        device=False, image=False, writes=None,
        summary="Describe every command as JSON."),
    ("plugins",): dict(
        device=False, image=False, writes=None,
        summary="List the installed plugins and their commands, and the "
                "first-party plugins that are not installed."),

    ("capture", "analyze"): dict(
        device=False, image=False, writes=None,
        summary="Show the structure of a recorded SysEx/MIDI capture. For "
                "audio, use `speech analyze`."),

    ("asset", "info"): dict(
        device=False, image=False, writes="file",
        summary="List what is in an MCC preset, sample or wavetable file. "
                "Writes files only with --extract."),

    ("asset", "import"): dict(
        device=False, image=False, writes="file",
        summary="Copy Arturia's factory presets, samples and wavetables "
                "from MIDI Control Center's installed files. The only "
                "source of factory samples. Offline."),
    ("config", "read"): dict(
        device=True, image=False, writes=None,
        summary="Read the instrument's 46 global settings; 31 have MCC "
                "names."),
    ("config", "write"): dict(
        device=True, image=False, writes="device",
        summary="Change one global setting, or restore a `config read "
                "--out` file. Asks first unless --yes. Each write is read "
                "back."),
    ("sample", "list"): dict(
        device=True, image=False, writes=None,
        summary="List every sample's name and length. Sample audio cannot "
                "be read back."),
    ("preset", "list"): dict(
        device=True, image=False, writes=None,
        summary="List every preset's name, in under a second, and which "
                "are empty."),
    ("preset", "backup"): dict(
        device=True, image=False, writes="file",
        summary="Save the presets as an MCC project (.mfprojz). Read-only "
                "on the device; restore with `preset restore`."),
    ("preset", "read"): dict(
        device=True, image=False, writes="file",
        summary="Read one preset. Writes an MCC .mfpz only with --out."),
    ("preset", "write"): dict(
        device=True, image=False, writes="device",
        summary="Overwrite a preset. Read or back it up first if you want "
                "it back. Asks first unless --yes."),
    ("preset", "restore"): dict(
        device=True, image=False, writes="device",
        summary="Write an MCC .mfprojz or .mfbz back to the instrument, up "
                "to all 512 presets. Asks first unless --yes."),
    ("preset", "erase"): dict(
        device=True, image=False, writes="device",
        summary="Empty one preset, a range, or all 512. Asks first "
                "unless --yes."),
    ("preset", "select"): dict(
        device=True, image=False, writes="device",
        summary="Switch the instrument to a preset. Destroys nothing."),
    ("play",): dict(
        device=True, image=False, writes="device",
        summary="Play a MIDI file on the instrument in real time, "
                "optionally selecting a preset first. Destroys nothing."),
    ("render",): dict(
        device=True, image=False, writes="device",
        summary="Play notes or a MIDI file on the instrument and record its "
                "audio output to a WAV. Needs an audio interface."),
    ("preset", "encode"): dict(
        device=False, image=False, writes="file",
        summary="Build an MCC .mfpz by changing named fields of a template. "
                "Offline with --template-file."),
    ("preset", "convert"): dict(
        device=False, image=False, writes="file",
        summary="Convert presets saved by older firmware to a newer "
                "firmware's format (--to, default 5.0): the engine for its "
                "list, and the missing parameters. Offline."),
    ("preset", "decode"): dict(
        device=False, image=False, writes=None,
        summary="Show every module and parameter in an MCC preset file "
                "(.mfpz, or one preset of a bank)."),

    ("sample", "encode"): dict(
        device=False, image=False, writes="file",
        summary="Convert a WAV into an MCC .mfsz. Checks its own output; a "
                "mismatch writes nothing."),
    ("sample", "write"): dict(
        device=True, image=False, writes="device",
        summary="Overwrite a sample. The old audio cannot be recovered. "
                "Asks first unless --yes."),
    ("sample", "erase"): dict(
        device=True, image=False, writes="device",
        summary="Delete one sample or a range, as MCC does. The audio "
                "cannot be recovered. Asks first unless --yes."),
    ("sample", "restore"): dict(
        device=True, image=False, writes="device",
        summary="Write an MCC .mfsbz to the instrument, each sample to its "
                "own number. Replaced audio cannot be recovered. Asks first "
                "unless --yes."),

    ("wavetable", "read"): dict(
        device=True, image=False, writes="file",
        summary="Read a wavetable off the instrument. Writes an MCC .mfwz "
                "only with --out."),
    ("wavetable", "write"): dict(
        device=True, image=False, writes="device",
        summary="Overwrite a wavetable. Asks first unless --yes."),
    ("wavetable", "list"): dict(
        device=True, image=False, writes=None,
        summary="List every wavetable's name, and which are empty."),
    ("wavetable", "backup"): dict(
        device=True, image=False, writes="file",
        summary="Save the wavetables as an MCC bank (.mfwbz). Read-only on "
                "the device; restore with `wavetable restore`."),
    ("wavetable", "restore"): dict(
        device=True, image=False, writes="device",
        summary="Write an MCC .mfwbz back to the instrument, each wavetable "
                "to its own number. Asks first unless --yes."),
    ("wavetable", "erase"): dict(
        device=True, image=False, writes="device",
        summary="Empty wavetables, as MCC does. Asks first unless --yes."),
    ("wavetable", "encode"): dict(
        device=False, image=False, writes="file",
        summary="Convert audio into an MCC .mfwz."),

    ("fw", "patch"): dict(
        device=False, image=True, writes="file",
        summary="Patch a stock 5.0.0.2084 .mff you supply with a plugin's "
                "firmware; --list names them. Writes a new .mff, not the "
                "instrument."),
    ("fw", "flash"): dict(
        device=True, image=True, writes="flash",
        summary="Install a .mff over USB DFU, every image in it unless "
                "--only. WRITES FLASH and can leave the instrument "
                "unresponsive. Asks first unless --yes; --plan sends "
                "nothing. Recovery: hold SHIFT+ARP+OCT/MOD at power-on."),
    ("fw", "probe"): dict(
        device=True, image=False, writes=None,
        summary="Report what the instrument says about itself over USB, "
                "and its firmware. Read-only."),
    ("fw", "enter"): dict(
        # writes="flash", not "device": it clears image 0's validity marker
        # in sector 2 and the instrument is unusable until power-cycled. It
        # was classified "device" and so fell outside the RULES line below
        # that says flash writers need confirmation -- which is how it ended
        # up the only device-writing command with no gate.
        device=True, image=False, writes="flash",
        summary="Reboot into DFU mode. Clears image 0's validity marker in "
                "flash; the instrument is unusable until power-cycled. "
                "Asks first unless --yes."),
    ("fw", "watch"): dict(
        device=True, image=False, writes=None,
        summary="Report when the instrument appears, disappears or changes "
                "mode on USB. Read-only."),
    ("fw", "info"): dict(
        device=False, image=True, writes=None,
        summary="Show a firmware file's version, images and checksums, and "
                "whether it is stock or patched."),
    ("fw", "unpack"): dict(
        device=False, image=True, writes="file",
        summary="Extract the images from a .mff."),
    ("fw", "repack"): dict(
        device=False, image=True, writes="file",
        summary="Write a new .mff with images replaced or left out."),
}

# Facts a caller needs and cannot get from any one command.
RULES = [
    "Presets are 1-512, samples 1-128, wavetables 1-16 -- the numbers the "
    "instrument shows, and what every command takes. The 0-based index the "
    "wire uses is reported as `slot` in --json alongside `panel`; do not "
    "pass it back in.",
    "MFREAK_EMULATOR=1 points every command that opens a port at "
    "microfreak-emu's emulator instead of hardware. It must be run from the "
    "microfreak-emu checkout, which is the environment that has both "
    "packages.",
    "With --json, stdout carries exactly one JSON object and all human "
    "output goes to stderr. Exit codes: 0 success, 1 failure, 2 usage.",
    "Commands whose writes field is \"flash\" can leave the instrument "
    "unresponsive and should never be run without explicit human "
    "confirmation. Recovery is SHIFT+ARP+OCT/MOD held at power-on.",
    "Plugins add commands under their own name, and a firmware patch for "
    "`fw patch NAME`. A plugin command whose summary says it needs patched "
    "firmware does nothing useful on stock firmware.",
]


def _check():
    """ROUTES and META must describe the same set of commands."""
    routes, meta = set(ROUTES), set(META)
    extra = meta - routes
    missing = routes - meta
    problems = []
    if missing:
        problems.append("in ROUTES but not classified in capabilities.META: "
                        + ", ".join(" ".join(p) for p in sorted(missing)))
    if extra:
        problems.append("classified in capabilities.META but not in ROUTES: "
                        + ", ".join(" ".join(p) for p in sorted(extra)))
    return problems


def flags_for(path):
    """-> the option strings this command's own parser accepts.

    Read from the parser itself by running it with --help and capturing the
    output, so this cannot fall out of step with the command.
    """
    if path == ("capabilities",):
        return ["--text", "--json"]
    module, prefix = routes()[path]
    try:
        mod = importlib.import_module(module)
    except Exception as exc:                        # a missing optional dep
        return ["<unavailable: %s>" % exc]
    buf = io.StringIO()
    saved = sys.argv
    sys.argv = ["mfreak " + " ".join(path)] + list(prefix) + ["--help"]
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                mod.main()
            except SystemExit:
                pass
            except Exception:
                pass
    finally:
        sys.argv = saved
    found = re.findall(r"(--[A-Za-z][A-Za-z0-9-]*)", buf.getvalue())
    return sorted(set(found) - {"--help"})


def meta():
    """-> META, with the installed plugins' commands."""
    from microfreak import plugin
    out = dict(META)
    for path, c in plugin.routes().items():
        out[path] = dict(device=c.device, image=c.image, writes=c.writes,
                         summary=c.summary or c.help)
    return out


def describe():
    commands = []
    every = meta()
    for path in routes():
        m = every.get(path, {})
        commands.append({
            "command": " ".join(path),
            "invoke": "mfreak " + " ".join(path),
            "needs_device": m.get("device"),
            "needs_firmware_image": m.get("image"),
            "writes": m.get("writes"),
            "summary": m.get("summary"),
            "flags": flags_for(path),
        })
    commands.sort(key=lambda c: c["command"])
    offline = [c["command"] for c in commands
               if c["needs_device"] is False and c["needs_firmware_image"] is False]
    return {
        "ok": True,
        "command": "capabilities",
        "tool": "mfreak",
        "version": __version__,
        "json_contract": {
            "stdout": "exactly one JSON object",
            "stderr": "all human-readable output",
            "exit_codes": {"0": "success", "1": "failure", "2": "usage"},
            "always_present": ["ok", "command", "exit"],
        },
        "writes_values": {
            "null": "reads or computes only",
            "file": "writes a file you named; safe for the instrument",
            "device": "changes the instrument's state",
            "flash": "writes FLASH; can leave the instrument unresponsive",
        },
        "needs_nothing": offline,
        "rules": RULES,
        "commands": commands,
        "warnings": _check(),
    }


def main():
    ap = argparse.ArgumentParser(
        prog="mfreak capabilities",
        description=__doc__.splitlines()[0],
        epilog="Generated from the command table, so it is always complete.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", action="store_true",
                    help="human-readable instead of JSON")
    ap.add_argument("--json", action="store_true",
                    help="JSON on stdout (the default for this command)")
    a = ap.parse_args()

    data = describe()
    code = 0 if not data["warnings"] else 1
    if jsonout.active():
        # `mfreak capabilities --json`: the dispatcher prints the object, so
        # contribute to it rather than printing a second one.
        jsonout.result(**{k: v for k, v in data.items()
                          if k not in ("ok", "command")})
        return code
    if not a.text:
        # `exit` too, so the one JSON-by-default command obeys the same
        # contract as every --json object. AGENTS.md and the README promise
        # every object carries ok/command/exit; the dispatcher adds `exit`
        # under --json, and this path bypasses the dispatcher, so a bare
        # `mfreak capabilities` was the single object missing it -- and its
        # own `json_contract.always_present` listed only ok and command,
        # which is the contract being described disagreeing with the docs
        # describing it.
        json.dump(dict(data, exit=code), sys.stdout, indent=1)
        sys.stdout.write("\n")
        return code

    print("mfreak %s -- %d commands\n" % (data["version"], len(data["commands"])))
    print("Needs no instrument and no firmware image:")
    for c in data["needs_nothing"]:
        print("    %s" % c)
    print()
    for c in data["commands"]:
        mark = {None: "       ", "file": "file   ", "device": "DEVICE ",
                "flash": "FLASH  "}.get(c["writes"], "?      ")
        need = []
        if c["needs_device"]:
            need.append("device")
        if c["needs_firmware_image"]:
            need.append("image")
        print("  %s %-24s %s" % (mark, c["command"],
                                 ("needs " + "+".join(need)) if need else ""))
        if c["summary"]:
            for line in _wrap(c["summary"], 68):
                print("              %s" % line)
    print("\nRules that are not any one command's business:")
    for r in data["rules"]:
        for i, line in enumerate(_wrap(r, 70)):
            print("  %s %s" % ("*" if i == 0 else " ", line))
    for w in data["warnings"]:
        print("\nWARNING: %s" % w)
    return 0 if not data["warnings"] else 1


def _wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


if __name__ == "__main__":
    sys.exit(main())

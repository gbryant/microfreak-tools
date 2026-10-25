#!/usr/bin/env python3
"""mfreak: tooling for the Arturia MicroFreak.

Each command is a module with its own `main()` and argument parsing. This
file only picks which one runs.
"""
import importlib
import sys

try:
    from importlib.metadata import version as _version
    __version__ = _version("microfreak-tools")
except Exception:               # running from a tree that was never installed
    __version__ = "unknown"

# command path -> (module, argv prefix to prepend)
#
# A group whose value is a single module with no prefix passes its own
# subcommands straight through to that module's parser.
ROUTES = {
    ("doctor",):            ("microfreak.doctor", []),
    ("capabilities",):      ("microfreak.capabilities", []),
    ("plugins",):           ("microfreak.plugin", []),

    # `capture analyze` reads a recorded SysEx/MIDI capture. It is NOT
    # `speech analyze`, which reads audio -- the two used to be one word
    # apart and were routinely confused.
    ("capture", "analyze"): ("microfreak.analyze", []),

    # `asset`, not `bank`: these read and import all three kinds of MCC
    # export -- presets, samples, wavetables -- and MCC calls them by kind.
    # `bank` was renamed on 2026-10-20, before the first public release.
    # The real-instrument half of `mfemu render --midi`: same file, same
    # parser, heard through the hardware instead of the emulator.
    ("play",):              ("microfreak.play", []),
    ("render",):            ("microfreak.render", []),

    ("asset", "info"):      ("microfreak.bank", []),
    ("asset", "import"):    ("microfreak.mcc", []),
    # The inverse of `asset info` for presets: a `preset backup` directory ->
    # a .mfprojz, which is what removes MCC from microfreak-emu's
    # prerequisites. Under `preset` because presets are all it packs.
    ("preset", "restore"):  ("microfreak.preset.restore", []),
    ("preset", "convert"):  ("microfreak.preset.convert", []),

    # MCC's DEVICE page, over MIDI. The write direction came out of
    # captures/device-settings.pcapng; see the module docstring.
    ("config", "read"):     ("microfreak.config", ["read"]),
    ("config", "write"):    ("microfreak.config", ["write"]),

    ("preset", "read"):     ("microfreak.preset.read", []),
    # `list` and `backup` are both the read path and nothing new on the wire.
    # The module is `listing` because `list` shadows the builtin.
    ("preset", "list"):     ("microfreak.preset.listing", []),
    ("preset", "backup"):   ("microfreak.preset.backup", []),
    ("preset", "write"):    ("microfreak.preset.write", []),
    ("preset", "erase"):    ("microfreak.preset.erase", []),
    ("preset", "select"):   ("microfreak.preset.select", []),
    ("preset", "encode"):   ("microfreak.preset.encode", []),
    # The module stays `microfreak.preset.tlv`; only the command is `decode`,
    # which says what it does to someone who has never heard of TLV.
    ("preset", "decode"):   ("microfreak.preset.tlv", []),

    ("sample", "list"):     ("microfreak.sample.listing", []),
    ("sample", "encode"):   ("microfreak.sample.encode", []),
    ("sample", "write"):    ("microfreak.sample.write", []),
    ("sample", "erase"):    ("microfreak.sample.erase", []),
    ("sample", "restore"):  ("microfreak.sample.restore", []),

    ("wavetable", "list"):    ("microfreak.wavetable.listing", []),
    ("wavetable", "read"):    ("microfreak.wavetable.read", []),
    ("wavetable", "backup"):  ("microfreak.wavetable.backup", []),
    ("wavetable", "write"):   ("microfreak.wavetable.write", []),
    ("wavetable", "restore"): ("microfreak.wavetable.restore", []),
    ("wavetable", "erase"):   ("microfreak.wavetable.erase", []),
    ("wavetable", "encode"):  ("microfreak.wavetable.encode", []),

    # The patches themselves come from plugins; see microfreak.plugin.
    ("fw", "patch"):        ("microfreak.firmware.patching", []),
    ("fw", "flash"):        ("microfreak.firmware.flash", ["flash"]),
    ("fw", "probe"):        ("microfreak.firmware.flash", ["probe"]),
    ("fw", "enter"):        ("microfreak.firmware.flash", ["enter"]),
    ("fw", "watch"):        ("microfreak.firmware.flash", ["watch"]),
    ("fw", "info"):         ("microfreak.firmware.mff", ["info"]),
    ("fw", "unpack"):       ("microfreak.firmware.mff", ["unpack"]),
    ("fw", "repack"):       ("microfreak.firmware.mff", ["repack"]),
}

SUMMARY = """usage: mfreak <command> [options]

  Start here:
    doctor                 find out why something isn't working
    plugins                list plugins, and the ones you can add

  Offline (no instrument needed):
    preset encode          build a preset from field values
    preset decode F.mfpz   show what is in a preset file
    preset convert F OUT   convert presets from older firmware for newer
    sample encode F.wav    convert audio to sample format
    wavetable encode       convert audio to wavetable format
    asset info F           show what is in an MCC export
    asset import --list    copy Arturia's factory content from MCC's files

  Reads the instrument:
    preset list            every preset name (under a second)
    preset backup F        save all 512 presets as an MCC project (about 50 s)
    preset read N          read one preset
    sample list            sample names, lengths and free space (about 12 s)
    wavetable list         every wavetable name
    wavetable backup F     save all 16 wavetables as an MCC bank
    wavetable read N       read one wavetable
    config read            the global settings
    fw probe               what the instrument reports over USB
    fw watch               report when it changes mode on USB

  Writes the instrument:
    preset write N         overwrite, copy or rename a preset
    preset restore F       write an MCC project or bank back
    preset erase N         empty a preset, a range, or all 512
    sample write N         upload a sample
    sample restore F       write an MCC sample bank
    sample erase N         delete a sample, or a range
    wavetable write N      overwrite, copy or rename a wavetable
    wavetable restore F    write an MCC wavetable bank back
    wavetable erase N      delete a wavetable, or a range
    config write           change a setting, or restore a saved dump

  Plays the instrument:
    preset select N        switch to a preset
    play F.mid             play a MIDI file on it
    render --out F.wav     play notes or a MIDI file and record the audio

  Firmware files (offline, needs a .mff you supply):
    fw info F              version, images, and whether it is patched
    fw patch NAME F        patch it with a plugin's firmware (--list)
    fw unpack F            take a .mff apart into its images
    fw repack F            put a .mff back together, images replaced

  Writes flash (can leave the instrument unresponsive):
    fw flash F             install a .mff over USB (--plan sends nothing)
    fw enter               reboot into the bootloader

  For developers:
    capture analyze F      decode a recorded SysEx/MIDI capture
    capabilities           every command, as JSON

  Numbers are the ones the instrument shows: presets 1-512, samples 1-128,
  wavetables 1-16. Commands that talk to the instrument are made for
  firmware 5.0; `mfreak doctor` shows what yours runs.

  Back up first: `preset backup F.mfprojz` and `wavetable backup F.mfwbz`.
  Sample audio cannot be read back, so keep the files you upload.
  If a flash goes wrong, hold SHIFT + ARP + OCT/MOD at power-on.

  Add --json to any command for one JSON object on stdout.
  Set MFREAK_EMULATOR=1 to talk to `mfemu serve` instead of hardware.

`mfreak <command> --help` shows that command's options.
"""

# What `mfreak GROUP --help` says above its list of commands.
GROUPS = {
    "preset": "Presets on the instrument, and in MCC files.",
    "sample": "Samples on the instrument, and in MCC files.\n\n"
              "Sample audio cannot be read back off the instrument, so keep "
              "the files\nyou upload.",
    "wavetable": "Wavetables on the instrument, and in MCC files.",
    "config": "The instrument's global settings, as on MCC's DEVICE page.",
    "asset": "MCC export files, and Arturia's factory content.",
    "capture": "Recorded SysEx/MIDI captures.",
    "fw": "Firmware files, and installing them.\n\n"
          "The stock firmware file is the 5.0.0.2084 .mff from the "
          "MicroFreak page\non arturia.com.",
    "peek": "Read the instrument's memory. Needs the peek firmware.",
}


def routes():
    """-> every command, the plugins' included: {path: (module, prefix)}."""
    from microfreak import plugin
    out = dict(ROUTES)
    out.update((k, (c.module, list(c.args)))
               for k, c in plugin.routes().items())
    return out


def summary():
    """-> SUMMARY, with the installed plugins' commands."""
    from microfreak import plugin
    lines = []
    for name, p in plugin.installed().items():
        if p.guide:
            lines.append("    %-22s %s" % (name, "how to use it, and more commands"))
        for sub, c in p.commands.items():
            if not c.advanced:
                lines.append("    %-22s %s" % ("%s %s" % (name, sub), c.help))
    for name, why in sorted(plugin.broken.items()):
        lines.append("    %-22s did not load: %s" % (name, why))
    missing = sorted(plugin.not_installed())
    block = ""
    if lines:
        block = "  Plugins:\n" + "\n".join(lines) + "\n"
    if missing:
        block += ("  Not installed: %s. `mfreak plugins` shows how to add "
                  "them.\n" % ", ".join(missing))
    marker = "  For developers:"
    return SUMMARY.replace(marker, block + "\n" + marker) if block else SUMMARY


def group_help(group, every):
    """-> `mfreak GROUP --help`: what the group is for, and each command in
    it with the line the top-level summary gives it."""
    import re
    lines = {}
    for line in summary().splitlines():
        m = re.match(r"    (\S+) (\S+)((?: \S+)*?)\s{2,}(\S.*)$", line)
        if m and m.group(1) == group:
            for sub in m.group(2).split("|"):
                lines[sub] = ("%s%s" % (sub, m.group(3)), m.group(4))
    subs = sorted({k[1] for k in every if len(k) == 2 and k[0] == group})
    out = [GROUPS.get(group, "mfreak %s" % group), ""]
    for sub in subs:
        usage, text = lines.get(sub, (sub, ""))
        out.append("  %-22s %s" % (usage, text))
    out += ["", "`mfreak %s <command> --help` shows that command's options."
            % group, ""]
    return "\n".join(out)


def _invoke(mod):
    """Call a command's main(), turning input-file errors into a refusal.

    A path that does not exist is a typo, not a bug, and it was reaching the
    user as a Python traceback from six different commands -- `preset
    decode`, `asset info`, `capture analyze`, `sample write`, `fw repack` and
    `speech analyze` all just opened the file. Translating here rather than
    at six call sites keeps the behaviour identical across all of them, and
    `sys.exit(str)` is the same refusal path argparse already uses, so
    `--json` records it as `error` without any further work.

    Only these are caught. Anything else still propagates with its traceback,
    because anything else IS a bug and hiding it would be worse.
    """
    try:
        return mod.main() or 0
    except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
        where = getattr(exc, "filename", None)
        raise SystemExit("%s%s" % ("%s: " % where if where else "",
                                   exc.strerror or exc))


def run(module, argv, prog, command=None):
    mod = importlib.import_module(module)
    saved = sys.argv
    sys.argv = [prog] + argv
    try:
        if command is None:
            return _invoke(mod)
        # --json: stdout carries one object, prose goes to stderr, and the
        # command itself needs no code for any of it. See microfreak.jsonout.
        from microfreak.jsonout import JsonOut
        with JsonOut(command) as out:
            return out.finish(_invoke(mod))
    finally:
        sys.argv = saved


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    # `--json` is global, like `--help`: handled here so that every command
    # honours it identically and none has to declare it. Stripped before the
    # command's own parser sees it. No command defines a `--json` of its own,
    # and none takes a value that could legitimately be the string "--json".
    want_json = "--json" in argv
    if want_json:
        argv = [a for a in argv if a != "--json"]

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(summary(), end="")
        return 0
    if argv[0] in ("-V", "--version"):
        print("mfreak %s" % __version__)
        return 0

    # Longest match first, so ("preset", "read") wins over ("preset",).
    for n in (2, 1):
        key = tuple(argv[:n])
        if key in ROUTES:
            module, prefix = ROUTES[key]
            # When the route supplies the subcommand name in `prefix`, the
            # target's own parser prints it, so prog must stop one level short
            # or the usage line reads "mfreak fw patch patch".
            shown = key[:-1] if prefix else key
            prog = " ".join(("mfreak",) + tuple(shown))
            return run(module, prefix + argv[n:], prog,
                       command=" ".join(key) if want_json else None)

    from microfreak import plugin
    commands = plugin.routes()
    key = tuple(argv[:2])
    if key in commands:
        c = commands[key]
        shown = key[:-1] if c.args else key
        return run(c.module, list(c.args) + argv[2:],
                   " ".join(("mfreak",) + shown),
                   command=" ".join(key) if want_json else None)

    group = argv[0]
    hint = plugin.missing(group)
    if hint:
        print("mfreak: %s" % hint, file=sys.stderr)
        return 2
    every = routes()
    subs = sorted({k[1] for k in every if len(k) == 2 and k[0] == group})
    guide = getattr(plugin.installed().get(group), "guide", None)
    if guide:
        if argv[1:2] in ([], ["-h"], ["--help"], ["help"]):
            print(guide, end="")
            return 0
        print("mfreak %s: no command %r\n" % (group, argv[1]), file=sys.stderr)
        print(guide, end="", file=sys.stderr)
        return 2
    if subs:
        if argv[1:2] in ([], ["-h"], ["--help"], ["help"]):
            print(group_help(group, every), end="")
            return 0
        print("mfreak %s: no command %r\n" % (group, argv[1]), file=sys.stderr)
        print(group_help(group, every), end="", file=sys.stderr)
        return 2
    groups = sorted({k[0] for k in every})
    print("mfreak: unknown command %r\nknown commands: %s\n\nTry `mfreak --help`."
          % (group, ", ".join(groups)), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

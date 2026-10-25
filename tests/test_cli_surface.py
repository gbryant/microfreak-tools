#!/usr/bin/env python3
"""The command surface itself: help text, exit codes, and the --json contract.

    uv run python tests/test_cli_surface.py

Needs no instrument and no firmware image -- it runs the CLI against bad
input and against its own documentation, so it is the one test that can run
anywhere, in about 4 s.

Each check is known to be able to fail, which was verified by breaking one
thing at a time: raising the help floor, dropping the documented `fw patch`
exception, feeding the bad-input check a traceback, and feeding the JSON
check something that is not JSON. All four turned the test red.

What it holds, and why each one is here rather than assumed:

1. **Every command has help worth reading.** `fw probe` shipped with an
   83-character `--help`: a usage line, `-h`, and nothing about what it does
   or that its output carries the serial number. A length floor catches that
   class of omission, which no amount of reading the code reveals.

2. **Bad input is refused, not crashed.** Seven commands answered a path
   typo with a Python traceback, because they simply opened the file.
   `cli._invoke` now translates those, and this keeps them translated.

3. **The README's own groups match `capabilities`.** The README sorts
   commands by what they can do to the instrument, and that list was written
   by hand while the command set kept growing: eight commands had drifted out
   of it. `capabilities.META` is the generated classification, so the README
   can be checked against it rather than proof-read.

4. **`--json` means exactly one object on stdout.** Including on failure,
   which is the case a caller actually has to handle.
"""
import io
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from microfreak.cli import ROUTES as BUILT_IN       # noqa: E402
from microfreak.cli import routes, summary          # noqa: E402
from microfreak.capabilities import META as BUILT_IN_META  # noqa: E402
from microfreak.capabilities import _check, meta    # noqa: E402

# Every command, the installed plugins' included.
ROUTES = routes()
META = meta()
SUMMARY = summary()

NOPE = "/nonexistent/definitely-not-here.bin"
HELP_FLOOR = 150          # characters; `fw probe` was 83

# Commands the README deliberately groups differently from capabilities.
GROUP_EXCEPTIONS = {}

# Every command must name itself -- `preset decode` used to print "mfreak
# preset tlv", a command that has not existed since it was renamed.
USAGE_EXCEPTIONS = {}

# EVERY command that can change the instrument is given arguments that make
# it refuse before it opens anything. This is not caution for its own sake:
# the sweep runs each command with no arguments, and
#
#     mfreak fw enter
#
# takes no required arguments and has no --yes gate, so it REBOOTED A REAL
# INSTRUMENT INTO ITS BOOTLOADER -- screen dark, Utility lit, MIDI gone --
# and it stayed there until the owner power-cycled it. A test must not be
# able to do that, so the rule is now structural rather than remembered:
# `test_no_destructive_command_runs_bare` fails if any command META marks as
# writing the device or flash is missing from this table.
#
# `preview` RECORDS FROM THE MICROPHONE, which a test must never do, and
# `fw watch` polls the USB bus for its default 180 seconds.
ARG_OVERRIDES = {
    # refuses in argparse, before any USB or MIDI
    "fw enter": ["--watch", "not-a-number"],
    "fw flash": ["/nonexistent/no-such.mff"],
    "fw patch": ["speech", "/nonexistent/no-such.mff", "--out", "/nonexistent/x.mff"],
    "preset write": ["99999"],
    "preset restore": ["/nonexistent/no-such-backup"],
    "preset erase": ["99999"],
    "preset select": ["99999"],
    # the file is checked before any port is opened
    "play": ["/nonexistent/no-such.mid"],
    "render": ["--midi", "/nonexistent/no-such.mid", "--out", "/nonexistent/x.wav"],
    "sample write": ["99999", "--file", "/nonexistent/x.mfsz"],
    "sample erase": ["99999"],
    "sample restore": ["/nonexistent/no-such.mfsbz"],
    "wavetable restore": ["/nonexistent/no-such.mfwbz"],
    "wavetable erase": ["99"],
    # 99999 alone: out of 1-16, refused by panel.to_slot before anything
    # opens. test 0 rejects any override naming a flag the command does not
    # accept, so an override cannot refuse for the wrong reason.
    "wavetable write": ["99999"],
    "config write": ["No Such Setting", "1"],
    "speech say": [],                 # needs text; refuses on its own
    "speech banks": [],               # needs --bank; refuses on its own
    "speech upload": [],              # needs an image; refuses on its own
    # harmless, but slow or invasive with no arguments
    "speech preview": ["--seconds", "not-a-number"],
    "fw watch": ["--seconds", "0"],
    # Bounded on purpose. Bare, these read every slot off the instrument --
    # 512 presets and 128 samples -- which the --json contract check does not
    # need and which made that check slow and timing-dependent when a device
    # is actually connected. Two slots exercise the same contract.
    "preset list": ["--from", "1", "--to", "2"],
    "sample list": ["--from", "1", "--to", "2"],
    "wavetable list": ["--from", "1", "--to", "2"],
}

# Commands that must refuse to touch the instrument without --yes, and the
# ones deliberately left ungated. `preset select` only changes which preset
# is showing. The `speech` three need firmware patched by `fw patch`, which
# is itself gated, and stock firmware has no upload path for them to use.
MUST_GATE = {
    "fw enter", "fw flash", "preset write", "preset restore", "preset erase",
    "sample write", "sample erase", "sample restore",
    "wavetable write", "wavetable restore", "wavetable erase", "config write",
}
UNGATED = {"preset select", "play", "render", "speech say", "speech banks",
           "speech upload"}

fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-54s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)


# `-c "import main; raise SystemExit(main())"`, NOT `-m microfreak.cli`, and the
# difference is load-bearing on supported interpreters.
#
# argparse derives the program name from `_sys.argv[0]` via `_prog_name()`,
# which this dispatcher sets to "mfreak" before building any parser. Under
# `-m`, `__main__.__spec__` names the module, and on Python 3.14.6 and
# 3.15.0b4 `_prog_name()` returns f'{py} -m {modname}' for that case
# UNCONDITIONALLY -- it never looks at argv[0]:
#
#     if modspec is None:                      # simple script
#         return _os.path.basename(arg0)
#     py = _os.path.basename(_sys.executable)
#     if modspec.name != '__main__':           # <- taken, argv[0] ignored
#         return f'{py} -m {modname}'
#
# so every command printed `usage: python3.14 -m microfreak.cli` and section 3
# failed on all 36. 3.14.7 and 3.15.0rc2 added a guard that restores the
# argv[0] branch -- `and arg0 != modspec.origin` -- so the `-m` form happens to
# pass there. requires-python is ">=3.11,<3.15", so 3.14.6 is supported and the
# suite must not depend on which patch release is installed.
#
# `-c` makes `__main__.__spec__` None on every version, which takes the
# `basename(arg0)` branch and honours the swap. That is also what the SHIPPED
# entry point does: a console script is a plain script, so `mfreak` itself
# prints the right usage line on 3.14.6 -- verified in a 3.14.6 venv. This form
# is therefore the closer proxy for how anyone actually runs the tool, and `-m`
# was testing an invocation no user types.
_VIA_MAIN = "from microfreak.cli import main; raise SystemExit(main())"


def mfreak(args, timeout=240):
    return subprocess.run([sys.executable, "-c", _VIA_MAIN] + args,
                          capture_output=True, text=True, timeout=timeout,
                          cwd=ROOT)


def test_no_destructive_command_runs_bare():
    print("\n0. nothing in this test can change the instrument")
    # Every device- or flash-writing command must have an entry above, so
    # that adding one later cannot quietly re-arm the sweep.
    risky = sorted(" ".join(k) for k, v in META.items()
                   if v["writes"] in ("device", "flash"))
    missing = [n for n in risky if n not in ARG_OVERRIDES]
    chk(not missing, "every device-writing command has safe arguments here",
        ", ".join(missing) or "%d commands" % len(risky))
    # And each of those argument sets must actually refuse: non-zero exit,
    # and nothing that looks like it reached the hardware.
    acted = []
    for name in risky:
        if name not in ARG_OVERRIDES:
            continue          # already reported above; do NOT run it bare
        r = mfreak(name.split() + ARG_OVERRIDES[name])
        out = r.stdout + r.stderr
        if r.returncode == 0 or "REBOOT" in out.upper() or "sending" in out:
            acted.append("%s exited %d" % (name, r.returncode))
    chk(not acted, "and each one refuses rather than acting",
        "; ".join(acted) or "%d commands" % len(risky))
    # And the gate itself. A command here is either gated on --yes or listed
    # as deliberately ungated -- a new one is in neither, so adding one
    # forces the decision instead of defaulting to no gate, which is exactly
    # how `fw enter` came to be the only ungated writer.
    undecided = [n for n in risky
                 if n not in MUST_GATE and n not in UNGATED]
    chk(not undecided, "every writing command is gated or listed as not",
        ", ".join(undecided) or "%d gated, %d not"
        % (len(MUST_GATE), len(UNGATED)))
    # --help is safe to run and is where the flag has to be visible anyway.
    # Anchored to the options listing on purpose. A plain substring search
    # passes on the prose in `fw enter`'s own description, which explains
    # the gate -- so it went green with the flag deleted.
    flag = re.compile(r"^\s+--yes\b", re.M)
    ungated = []
    for n in sorted(MUST_GATE):
        r = mfreak(n.split() + ["--help"])
        if not flag.search(r.stdout + r.stderr):
            ungated.append(n)
    chk(not ungated, "and each gated command's --help offers --yes",
        ", ".join(ungated) or "%d commands" % len(MUST_GATE))

    # Every FLAG an override passes must exist on that command. An override
    # that refuses on an unknown flag rather than on the bad input it was
    # meant to test keeps passing after that check is deleted.
    bogus = []
    for name, argv in sorted(ARG_OVERRIDES.items()):
        flags = [a for a in argv if a.startswith("--")]
        if not flags:
            continue
        r = mfreak(name.split() + ["--help"])
        help_text = r.stdout + r.stderr
        for f in flags:
            if not re.search(r"%s\b" % re.escape(f), help_text):
                bogus.append("%s has no %s" % (name, f))
    chk(not bogus, "and every flag an override passes really exists",
        "; ".join(bogus) or "%d overrides" % len(ARG_OVERRIDES))


def test_routes_and_meta():
    print("\n1. the command table")
    chk(not _check(), "ROUTES and capabilities.META describe the same set",
        "; ".join(_check()) or "%d commands" % len(ROUTES))


def test_help():
    print("\n2. every command's --help")
    thin, broken = [], []
    for key in sorted(ROUTES):
        r = mfreak(list(key) + ["--help"])
        out = r.stdout + r.stderr
        if r.returncode != 0 or "usage:" not in out or "Traceback" in out:
            broken.append(" ".join(key))
        elif len(out.strip()) < HELP_FLOOR:
            thin.append("%s (%d chars)" % (" ".join(key), len(out.strip())))
    chk(not broken, "every --help runs and prints a usage line",
        ", ".join(broken) or "%d commands" % len(ROUTES))
    chk(not thin, "no --help is under %d characters" % HELP_FLOOR,
        ", ".join(thin) or "thinnest is above the floor")


def test_usage_names_the_command():
    print("\n3. usage lines")
    wrong = []
    for key in sorted(ROUTES):
        name = " ".join(key)
        r = mfreak(list(key) + ["--help"])
        first = (r.stdout + r.stderr).splitlines()[0]
        want = USAGE_EXCEPTIONS.get(name, "mfreak %s" % name)
        if not first.startswith("usage: %s" % want):
            wrong.append("%s prints %r" % (name, first[:48]))
    chk(not wrong, "every --help names the command you typed",
        "; ".join(wrong) or "%d commands" % len(ROUTES))


def test_summary_lists_everything():
    print("\n4. the top-level help")
    from microfreak import plugin
    guides = {n: p.guide or "" for n, p in plugin.installed().items()}
    missing = []
    for key in sorted(ROUTES):
        name = " ".join(key)
        if name in guides.get(key[0], ""):
            continue                    # an advanced command, in its guide
        if name not in SUMMARY and not re.search(
                r"%s[^\n]*\b%s\b" % (re.escape(key[0]), re.escape(key[-1])),
                SUMMARY):
            missing.append(name)
    chk(not missing, "`mfreak` with no arguments names every command",
        ", ".join(missing) or "%d commands" % len(ROUTES))


def test_readme_groups():
    print("\n5. the README's classification")
    readme = io.open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()

    def group(title):
        m = re.search(r"\*\*%s\*\*(.*?)(?=\n\n)" % re.escape(title),
                      readme, re.S)
        return set(re.findall(r"`([a-z]+(?: [a-z-]+)?)`", m.group(1))) if m else set()

    doc = {"offline": group("Needs nothing plugged in."),
           "reads": group("Reads the instrument."),
           "writes": group("Writes the instrument."),
           "flash": group("Writes flash.")}

    def expected(info):
        if info["writes"] == "flash":
            return "flash"
        if not info["device"]:
            return "offline"
        return "writes" if info["writes"] == "device" else "reads"

    wrong = []
    for key, info in sorted(META.items()):
        name = " ".join(key)
        want = GROUP_EXCEPTIONS.get(name, expected(info))
        where = [g for g, names in doc.items() if name in names]
        if where != [want]:
            wrong.append("%s: %s, not %s" % (name, where or "missing", want))
    chk(not wrong, "every command is grouped as capabilities classifies it",
        "; ".join(wrong) or "%d commands" % len(META))


def test_bad_input_is_refused():
    print("\n6. bad input")
    cases = [
        ("preset read", ["99999"]), ("preset select", ["99999"]),
        ("preset write", ["99999"]), ("preset list", ["--from", "99999"]),
        ("preset decode", [NOPE]), ("sample list", ["--from", "999"]),
        ("sample write", ["0", "--file", NOPE]),
        ("preset write", ["1", "--file", NOPE]),
        ("preset decode", ["/tmp/x.bin"]),
        ("preset convert", [NOPE, "/tmp/x.mfpz"]),
        ("wavetable write", ["1", "--file", NOPE]),
        ("wavetable read", ["999"]), ("asset info", [NOPE]),
        ("capture analyze", [NOPE]), ("speech analyze", [NOPE]),
        ("fw flash", [NOPE, "--plan"]), ("fw info", [NOPE]),
        ("fw repack", [NOPE, "--out", "/tmp/x.mff"]),
        ("fw flash", [NOPE]), ("config write", ["No Such Setting", "1"]),
    ]
    crashed, silent = [], []
    for name, args in cases:
        r = mfreak(name.split() + args)
        out = r.stdout + r.stderr
        if "Traceback (most recent call last)" in out:
            crashed.append("%s %s" % (name, " ".join(args)))
        elif r.returncode == 0 or not out.strip():
            silent.append(name)
    chk(not crashed, "no command answers bad input with a traceback",
        ", ".join(crashed) or "%d cases" % len(cases))
    chk(not silent, "every refusal says something and exits non-zero",
        ", ".join(silent) or "%d cases" % len(cases))


def test_json_contract():
    print("\n7. the --json contract")
    # The top-level help says --json works on EVERY command, so every command
    # is tested, not a sample. Most are run with no arguments and so fail --
    # which is the case that matters, because a caller has to parse the
    # object precisely when the command did not work.
    cases = [(" ".join(k), ARG_OVERRIDES.get(" ".join(k), []))
             for k in sorted(ROUTES)]
    cases += [("preset decode", [NOPE]), ("asset info", [NOPE]),
              ("preset read", ["99999"])]
    bad = []
    for name, args in cases:
        r = mfreak(name.split() + args + ["--json"])
        try:
            obj = json.loads(r.stdout)
        except Exception as exc:
            # Report what actually arrived. This used to say only "stdout is
            # not one object", which on an intermittent failure against a real
            # instrument is unactionable -- you cannot tell an empty stdout
            # from a traceback from two objects without re-running and hoping.
            bad.append("%s: stdout is not one object (%s); exit %d, "
                       "%d bytes of stdout %r, stderr tail %r"
                       % (name, str(exc)[:30], r.returncode, len(r.stdout),
                          r.stdout[:120], r.stderr[-160:]))
            continue
        for field in ("ok", "command", "exit"):
            if field not in obj:
                bad.append("%s: no %r" % (name, field))
        if obj.get("command") != name:
            bad.append("%s: command=%r" % (name, obj.get("command")))
        if obj.get("ok") is not (r.returncode == 0):
            bad.append("%s: ok=%r, exit=%r" % (name, obj.get("ok"), r.returncode))
    chk(not bad, "stdout is one JSON object for every command, pass or fail",
        "; ".join(bad) or "%d cases over %d commands" % (len(cases), len(ROUTES)))


def test_switch_tables_are_ascending():
    """switch_name scans `if raw <= entry.value: return entry.name`, so a
    table that is not ascending mis-decodes silently. `arp_seq_mod` was
    [17408, 10922, 21845, 32767] -- out of order, and 17408 where the even
    grid every other table follows puts 0.
    """
    print("\n8. switch value tables")
    from microfreak.preset.fields import SWITCH
    unsorted_, off_grid = [], []
    for key, spec in SWITCH.items():
        nums = [e["value"] for e in spec["values"]]
        if any(b <= a for a, b in zip(nums, nums[1:])):
            unsorted_.append("%s %s" % (key, nums))
        if len(nums) > 1:
            grid = [round(i * 0x7FFF / (len(nums) - 1)) for i in range(len(nums))]
            if any(abs(a - b) > 1 for a, b in zip(nums, grid)):
                off_grid.append("%s %s" % (key, nums))
    chk(not unsorted_, "every switch table ascends, as switch_name requires",
        "; ".join(unsorted_) or "%d tables" % len(SWITCH))
    chk(not off_grid, "and every one sits on its even 0..0x7FFF grid",
        "; ".join(off_grid) or "%d tables" % len(SWITCH))


def test_bank_json_slot_is_zero_based():
    """`asset info` JSON must use the same slot/panel convention as every
    other command: "slot" 0-based, "panel" = slot + 1. Item.slot is 1-based,
    so this emitted slot=panel and panel=slot+1 -- a caller feeding "slot"
    to `preset write` wrote to the NEXT preset.
    """
    print("\n9. asset info slot convention")
    bank = os.path.join(os.path.dirname(ROOT), "synth-helper", "data",
                        "banks", "factory_5_0_new_presets.mfprojz")
    if not os.path.exists(bank):
        chk(True, "asset info slot/panel convention", "skipped: no bank file")
        return
    r = mfreak(["asset", "info", bank, "--json"])
    rows = json.loads(r.stdout)["items"]
    chk(all(x["panel"] == x["slot"] + 1 for x in rows),
        "panel is slot + 1 in every row", "%d rows" % len(rows))
    chk(rows and rows[0]["slot"] == 0,
        "the first bank item is slot 0, not slot 1",
        "slot=%s" % (rows[0]["slot"] if rows else "?"))


def test_agents_md_needs_nothing():
    """AGENTS.md names the offline commands in prose, and the prose drifted:
    it once said ten and omitted a command. The generated
    list in capabilities.META is the truth; this pins the copy to it.
    """
    print("\n10. AGENTS.md offline list")
    # The built-in commands only: AGENTS.md lists plugins' separately.
    want = sorted(" ".join(k) for k, v in BUILT_IN_META.items()
                  if not v["device"] and not v["image"])
    text = open(os.path.join(ROOT, "AGENTS.md")).read()
    para = text.split("## Working with no instrument", 1)[1].split("\n\n")[1]
    # Only the list sentence, not the note after it.
    listing = para.split("That list is generated", 1)[0]
    named = sorted(re.findall(r"`([a-z]+(?: [a-z]+)?)`", listing))
    named = [n for n in named if n in {" ".join(k) for k in BUILT_IN_META}]
    missing = [c for c in want if c not in named]
    extra = [c for c in named if c not in want]
    chk(not missing and not extra,
        "AGENTS.md names exactly the commands that need nothing",
        ("missing %s" % missing if missing else "") +
        ("; extra %s" % extra if extra else "") or "%d commands" % len(want))
    count = re.search(r"^(\w+) commands need nothing", para)
    words = {"Nine": 9, "Ten": 10, "Eleven": 11, "Twelve": 12, "Thirteen": 13}
    chk(count is not None and words.get(count.group(1)) == len(want),
        "and the number it spells out matches",
        "%s vs %d" % (count.group(1) if count else "?", len(want)))


def test_verify_mismatch_exits_nonzero():
    print("\n11. a failed read-back is not reported as success")
    # AGENTS.md states the rule and `sample write` / `wavetable write` both
    # obeyed it, but `preset write` never did: it printed "verify MISMATCH",
    # emitted verified=False, then fell off the end of main(). cli._invoke
    # does `return mod.main() or 0`, so None became exit 0 and a corrupted
    # preset reported clean. Fixing the three in place does not stop a
    # fourth writer from landing the same way, so this reads the source.
    #
    # Source-level on purpose: the live path needs an instrument AND a write
    # that actually fails, which cannot be provoked on demand. It finds its
    # own subjects rather than naming files, so a new writer is covered the
    # day it is added. Note the subject is the literal `verified=False`:
    # `config write` computes its verdict into a variable and is checked by
    # eye instead -- it returns `1 if failed else 0`.
    subjects = []
    for base, _, names in os.walk(os.path.join(ROOT, "microfreak")):
        if "__pycache__" in base:
            continue
        for n in sorted(names):
            if not n.endswith(".py"):
                continue
            path = os.path.join(base, n)
            lines = open(path).read().splitlines()
            for i, l in enumerate(lines):
                if "verified=False" in l:
                    subjects.append((os.path.relpath(path, ROOT), i, lines))
    chk(len(subjects) >= 3, "found the writers that report a verdict",
        "%d call site(s)" % len(subjects))
    for rel, i, lines in subjects:
        indent = len(lines[i]) - len(lines[i].lstrip())
        escapes = False
        for l in lines[i + 1:]:
            if l.strip() and (len(l) - len(l.lstrip())) < indent:
                break         # left the block without returning
            if l.strip() in ("return 1", "return 2") or "sys.exit(" in l:
                escapes = True
                break
        chk(escapes, "%s:%d verified=False then exits non-zero" % (rel, i + 1),
            "" if escapes else "falls through, which cli.py turns into exit 0")


def test_slot_arguments_are_panel_numbers():
    print("\n12. commands take the number on the instrument, not the index")
    # The instrument has presets 1-512; the wire counts from 0. Which one the
    # CLI takes was inverted on 2026-10-19, and the two ranges overlap almost
    # entirely -- 1-511 is valid either way and means a different preset each
    # way -- so nothing but a test keeps a command from drifting back.
    #
    # `0` is the one value that distinguishes them, which is why it has its
    # own message rather than a bare range error.
    for name, args, what in (
            ("preset read", ["0"], "preset"),
            ("preset select", ["0"], "preset"),
            ("preset write", ["0"], "preset"),
            ("sample write", ["0", "--file", "/nonexistent/x.mfsz"],
             "sample"),
            ("wavetable read", ["0"], "wavetable"),
            ("wavetable write", ["0"], "wavetable"),
    ):
        r = mfreak(name.split() + args)
        out = (r.stdout + r.stderr).lower()
        ok = r.returncode == 2 and "there is no %s 0" % what in out
        chk(ok, "%s refuses 0 and says why" % name,
            "" if ok else "exit %d: %s" % (r.returncode, out.strip()[-90:]))

    # And the top of the range must be the panel number, not the index: 512 is
    # a real preset and 513 is not. Checked through --help-free argparse only,
    # so neither reaches the instrument -- both die on the missing --yes or on
    # the range, and only the range answer is a usage error about the number.
    for name, hi, over in (("preset read", 512, 513), ("wavetable read", 16, 17)):
        r = mfreak(name.split() + [str(over)])
        bad = r.returncode == 2 and "1-%d" % hi in (r.stdout + r.stderr)
        chk(bad, "%s refuses %d as out of 1-%d" % (name, over, hi),
            "" if bad else "exit %d" % r.returncode)

    # No command should advertise a 0-based SLOT in its own --help any more.
    # Not a bare search for "0-127": that is also the MIDI value range, which
    # `config read` and `preset select` both discuss legitimately -- the first
    # version of this check flagged them and found one real leak hiding among
    # them ("Factory only" blocks slots 0-127). So: slot-shaped ranges, and
    # the word slot next to a zero-based one.
    stale = re.compile(r"0-511|0-15\b|0-based|0-indexed|slots? 0-\d")
    bad = []
    for path in sorted(META):
        r = mfreak(list(path) + ["--help"])
        for m in set(stale.findall(r.stdout + r.stderr)):
            bad.append("%s says %r" % (" ".join(path), m))
    chk(not bad, "no --help advertises a 0-based slot",
        "; ".join(sorted(bad)) or "%d commands" % len(META))


def test_counts_and_flash_writers_match_the_code():
    print("\n13. numbers in the prose match the code")
    # Three figures that had drifted and were caught by review rather than by
    # anything here. All are derived from META/ROUTES, so a new command moves
    # them and this fails until the prose is updated. The prose counts the
    # built-in commands, which do not depend on what plugins are installed.
    n = len(BUILT_IN)
    # Spelled out rather than looked up in a table. This was
    # {30: "thirty", 36: "thirty-six", 40: "forty"}, so adding the 37th
    # command made the check report `?` as the expected word -- the test
    # needed hand-editing for every command, which is the maintenance this
    # section exists to remove.
    _TENS = {2: "twenty", 3: "thirty", 4: "forty", 5: "fifty",
             6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety"}
    _UNITS = ["", "one", "two", "three", "four", "five", "six", "seven",
              "eight", "nine"]

    def spell(k):
        if not 20 <= k <= 99:
            return "?"          # no command set this project will ever have
        tens, units = divmod(k, 10)
        return _TENS[tens] + ("-" + _UNITS[units] if units else "")

    words = {n: spell(n)}
    for rel, pattern, what in (
            ("README.md", r"all (\d+) commands", "README's command count"),
            ("microfreak/jsonout.py", r"holds for all ([a-z-]+)", "jsonout's count"),
    ):
        text = open(os.path.join(ROOT, rel)).read()
        m = re.search(pattern, text)
        got = m.group(1) if m else None
        want = str(n) if rel == "README.md" else words.get(n, "?")
        chk(got == want, "%s says %s" % (what, want), "found %r" % got)

    # AGENTS.md's hard rule must name exactly the writes="flash" commands.
    # It named `fw patch` (which is "file") and omitted `fw enter` (which is
    # "flash" and is the one that strands the instrument), so an agent
    # obeying it gated the wrong command.
    flash = {" ".join(k) for k, v in META.items() if v["writes"] == "flash"}
    agents = open(os.path.join(ROOT, "AGENTS.md")).read()
    i = agents.index('Never run a command whose `writes` is `"flash"`')
    para = agents[i:i + 1200]
    named = {m for m in re.findall(r"`(fw [a-z-]+)`", para)}
    missing = flash - named
    chk(not missing, "AGENTS.md's flash rule names every flash writer",
        ", ".join(sorted(missing)) or ", ".join(sorted(flash)))


# Flags named in help PROSE that belong to another tool, not to this one, as
# (command, flag). Everything else must be a flag of the command whose help
# mentions it, or of one of its subcommands.
FOREIGN_FLAGS = set()

_OPT_LINE = re.compile(r"^\s+(?:-\w,\s+)?(--[a-z][a-z0-9-]*)", re.M)


def test_help_mentions_no_flag_that_was_renamed_away():
    print("\n14. help text names only flags that exist")
    # Two docstrings advertised `--from-slot` after the flag was renamed to
    # `--from`, and the emulator's docs advertised `--template-slot` for
    # `preset encode`, which takes `--template-preset`. A rename updates the
    # add_argument call, the --help listing follows automatically, and the
    # PROSE above it keeps the old name -- so the one place a user reads
    # first is the one place that goes stale, silently.
    #
    # The rule is tool-wide rather than per-command on purpose: help text
    # legitimately cross-references other commands (`fw patch --image`), so
    # requiring the flag on THAT command would be wrong. Requiring it to
    # exist SOMEWHERE still catches every rename, which is the failure here.
    real = set()
    helps = {}
    for key in sorted(ROUTES):
        r = mfreak(list(key) + ["--help"])
        helps[" ".join(key)] = h = r.stdout + r.stderr
        real |= set(_OPT_LINE.findall(h))
        # Subcommand flags count: a group's help can show a flag that lives
        # on one of its subcommands. The usage line lists them as {a,b,c}.
        for grp in re.findall(r"\{([a-z0-9,\-]+)\}", h):
            for sub in grp.split(","):
                r = mfreak(list(key) + [sub, "--help"])
                real |= set(_OPT_LINE.findall(r.stdout + r.stderr))
    orphans = []
    for name, h in sorted(helps.items()):
        for flag in sorted(set(re.findall(r"(--[a-z][a-z0-9-]{2,})", h))):
            if flag not in real and (name, flag) not in FOREIGN_FLAGS:
                orphans.append("%s mentions %s" % (name, flag))
    chk(not orphans, "every flag any --help names exists somewhere",
        "; ".join(orphans) or "%d flags across %d commands"
        % (len(real), len(helps)))


def main():
    print(__doc__.splitlines()[0])
    for t in (test_no_destructive_command_runs_bare,
              test_routes_and_meta, test_help, test_usage_names_the_command,
              test_summary_lists_everything, test_readme_groups,
              test_bad_input_is_refused, test_json_contract,
              test_switch_tables_are_ascending,
              test_bank_json_slot_is_zero_based,
              test_agents_md_needs_nothing,
              test_verify_mismatch_exits_nonzero,
              test_slot_arguments_are_panel_numbers,
              test_counts_and_flash_writers_match_the_code,
              test_help_mentions_no_flag_that_was_renamed_away):
        t()
    print("\n%s" % ("FAIL - %d check(s) failed" % len(fails) if fails
                    else "PASS - the command surface matches its documentation"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

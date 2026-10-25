"""Plugins: packages that add a firmware patch and the commands that use it.

A plugin declares one entry point in the `mfreak.plugins` group:

    [project.entry-points."mfreak.plugins"]
    speech = "mfreak_speech:plugin"

naming a `Plugin`. Its name is the command word it owns, `mfreak speech ...`,
and the name its patch takes in `mfreak fw patch speech ...`. A patch module
defines `build(img, args)`, which patches the main image's code in place and
prints what it did, and optionally `add_arguments(parser)`. The identity check
runs before `build` and the file is written after it; the building blocks are
in `microfreak.firmware.patching`.
"""
import importlib
import os
import sys
from dataclasses import dataclass, field

GROUP = "mfreak.plugins"


@dataclass
class Command:
    """`mfreak <plugin> <name>` runs `module`'s main() with `args` first."""
    module: str
    args: list = field(default_factory=list)
    help: str = ""             # one line for `mfreak --help`
    advanced: bool = False     # listed in the plugin's guide, not `mfreak --help`
    # For `mfreak capabilities`:
    device: bool = False       # needs an instrument
    image: bool = False        # needs a firmware file the user supplies
    writes: str = None         # None, "file", "device" or "flash"
    summary: str = ""


@dataclass
class Firmware:
    module: str
    help: str                  # one line for `mfreak fw patch --list`
    # One letter, A-Z or a-z, that the patched build answers opcode 0x48 with,
    # alongside its revision: S for speech. Pick one no other plugin uses;
    # KNOWN_IDENTITY lists the first-party ones.
    identity: str = None

    def load(self):
        return importlib.import_module(self.module)


@dataclass
class Plugin:
    name: str
    help: str
    commands: dict = field(default_factory=dict)
    firmware: Firmware = None
    # A module for `mfreak doctor`. Its offline(report) runs with the offline
    # checks, and device(report, midi_out, midi_in, args, reply) once the
    # instrument has answered the Device Inquiry with `reply`. Either may be
    # left out. Neither may change anything.
    doctor: str = None
    # What `mfreak <name>` prints with no command: how to use the plugin.
    guide: str = None
    package: str = ""          # filled in from the distribution when loaded
    version: str = ""


# First-party plugins, listed when they are not installed.
KNOWN = {
    "speech": ("mfreak-speech",
               "put your own words on the Speech engine"),
    "peek": ("mfreak-peek",
             "read the instrument's memory, and dump your bootloader"),
}

# Identity letters of the first-party patches, so an instrument running one
# is named even when its plugin is not installed.
KNOWN_IDENTITY = {"S": "speech", "P": "peek", "W": "words"}

_loaded = None
broken = {}                    # name -> why it did not load


def installed():
    """-> {name: Plugin} for every plugin that loads."""
    global _loaded
    if _loaded is not None:
        return _loaded
    from importlib.metadata import entry_points
    _loaded = {}
    for ep in entry_points(group=GROUP):
        try:
            p = ep.load()
            if not isinstance(p, Plugin):
                raise TypeError("%s is not a microfreak.plugin.Plugin" % ep.value)
        except Exception as exc:
            broken[ep.name] = "%s: %s" % (type(exc).__name__, exc)
            continue
        if p.name != ep.name:
            broken[ep.name] = "its Plugin is named %r" % p.name
            continue
        from microfreak.cli import ROUTES
        if any(k[0] == p.name for k in ROUTES):
            broken[ep.name] = "`%s` is already a built-in command" % p.name
            continue
        if ep.dist is not None:
            p.package, p.version = ep.dist.name, ep.dist.version
        if p.firmware and p.firmware.identity is not None:
            ident = p.firmware.identity
            clash = [n for n, q in _loaded.items()
                     if q.firmware and q.firmware.identity == ident]
            if KNOWN_IDENTITY.get(ident, p.name) != p.name:
                clash.append(KNOWN_IDENTITY[ident])
            letter = (isinstance(ident, str) and len(ident) == 1
                      and ident.isascii() and ident.isalpha())
            if not letter or clash:
                broken[ep.name] = ("its identity %r is %s" % (
                    ident, "taken by " + clash[0] if clash else "not one letter"))
                continue
        _loaded[p.name] = p
    return _loaded


def firmwares():
    return {n: p for n, p in installed().items() if p.firmware}


def routes():
    """-> {(plugin, command): Command}"""
    return {(n, c): cmd for n, p in installed().items()
            for c, cmd in p.commands.items()}


def not_installed():
    """-> {name: (package, help)} for first-party plugins not installed."""
    have = installed()
    return {n: v for n, v in KNOWN.items() if n not in have}


class Inquiry(bytes):
    """A Device Inquiry reply, with `who`: the (letter, rev) a patched build
    gave when asked with WHO_OPCODE, or None."""
    who = None


def answered(reply, who):
    """-> `reply` as an Inquiry carrying `who`."""
    out = Inquiry(reply)
    out.who = who
    return out


def looks_stock(reply):
    """True when the reply names stock 5.0.0.2084, which a patched build also
    answers. Only then is WHO_OPCODE worth asking."""
    from microfreak.firmware.patching import STOCK_BUILD
    return reply is not None and tuple(reply[12:16]) == (5, 0, 0, STOCK_BUILD)


def inquire(synth, wait=0.4, tries=3):
    """Device Inquiry, then WHO_OPCODE if the reply looks stock.
    -> an Inquiry, or None if the instrument does not answer.

    Each request is sent up to `tries` times: the first one after opening the
    port sometimes gets no reply, and asking again gets one."""
    from microfreak.firmware.peek import (INQUIRY_REPLY_PREFIX, INQUIRY_REQUEST,
                                          who_message, who_reply)
    slow = 5 if os.environ.get("MFREAK_EMULATOR") else 1   # slower than real time

    def ask(msg, match, timeout, times):
        for _ in range(times):
            synth.send(list(msg))
            try:
                return bytes(synth.recv(match, timeout=timeout * slow))
            except TimeoutError:
                pass
        return None

    reply = ask(INQUIRY_REQUEST,
                lambda m: bytes(m).startswith(INQUIRY_REPLY_PREFIX), 2.0, tries)
    if reply is None:
        return None
    who = None
    if looks_stock(reply):
        # Stock never answers, so this costs `wait` twice there.
        got = ask(who_message(0x5A),
                  lambda m: who_reply(bytes(m), 0x5A) is not None, wait, 2)
        who = got and who_reply(got, 0x5A)
    return answered(reply, who)


def running(reply):
    """-> (what, rev) an instrument's Device Inquiry `reply` says it runs.
    `what` is "stock", "official" for any other Arturia version, a plugin's
    name, or None for a letter nothing claims. `rev` is a patch's revision,
    else None. A patched build answers as stock and names itself in
    `reply.who` (see inquire); older builds put their letter in the reply."""
    if reply is None or len(reply) < 16:
        return None, None
    major, minor, patch, build = reply[12:16]
    who = getattr(reply, "who", None)
    if looks_stock(reply):
        if who is None:
            return "stock", None
        letter, build = who
    else:
        letter = chr(patch)
        if (major, minor) != (5, 0) or not (letter.isascii() and letter.isalpha()):
            return "official", None
    for n, p in installed().items():
        if p.firmware and p.firmware.identity == letter:
            return n, build
    return KNOWN_IDENTITY.get(letter), build


def install_hint(name):
    package = KNOWN[name][0]
    return ("    uv tool install --reinstall microfreak-tools --with %s\n"
            "Add a --with for each plugin to keep. From a microfreak-tools "
            "checkout:\n"
            "    uv tool install --reinstall . --with ./plugins/%s"
            % (package, name))


def missing(name):
    """-> a message if `name` is a plugin that is not installed, else None."""
    if name in broken:
        return "the %s plugin is installed but did not load: %s" % (
            name, broken[name])
    if name in KNOWN and name not in installed():
        return ("`%s` comes from the %s plugin, which is not installed.\n%s"
                % (name, KNOWN[name][0], install_hint(name)))
    return None


def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="List the installed plugins, and the ones you can add.",
        epilog="A plugin adds a firmware patch for `mfreak fw patch` and the "
               "commands that\nuse it.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    from microfreak import jsonout
    have = installed()
    for name, p in sorted(have.items()):
        print("%-10s %s  (%s %s)" % (name, p.help, p.package or "?", p.version))
        if p.firmware:
            print("  fw patch %-12s %s" % (name, p.firmware.help))
        for c, cmd in p.commands.items():
            print("  %-21s %s" % ("%s %s" % (name, c), cmd.help))
    for name, why in sorted(broken.items()):
        print("%-10s did not load: %s" % (name, why), file=sys.stderr)
    missing_ = not_installed()
    if missing_:
        if have:
            print()
        print("not installed:")
        for name, (package, text) in sorted(missing_.items()):
            print("  %-10s %s  (%s)" % (name, text, package))
        print("\nTo add one, for example %s:" % sorted(missing_)[0])
        for line in install_hint(sorted(missing_)[0]).splitlines():
            print("  " + line)
    jsonout.result(
        installed=[dict(name=n, package=p.package, version=p.version,
                        firmware=bool(p.firmware), commands=sorted(p.commands))
                   for n, p in sorted(have.items())],
        not_installed=[dict(name=n, package=v[0])
                       for n, v in sorted(missing_.items())],
        broken=broken)
    return 1 if broken else 0

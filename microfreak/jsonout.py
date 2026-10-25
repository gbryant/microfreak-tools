#!/usr/bin/env python3
"""`--json`, defined once for every command.

`--json` is a GLOBAL option, handled by `microfreak.cli` before a command ever
sees it -- like `--help`. No command declares it and no command can forget it,
so the contract below holds for all forty, and the plugins' commands,
including the ones that write flash.

The contract:

  * **stdout carries exactly one JSON object and nothing else.** Not a log
    line, not a progress bar, not a warning. A caller can `json.loads()`
    stdout without filtering it.
  * **Every human line the command would have printed goes to stderr**
    instead. Nothing is lost; it moves.
  * **Exit codes are unchanged** by `--json`: 0 success, 1 the operation
    failed, 2 bad usage. A machine reads the code first and the object second.
  * The object always carries `ok` (bool), `command` (str) and `exit` (int),
    so a caller can branch without knowing which command it ran.

Why the stream is redirected rather than every `print()` rewritten: these
commands print a lot of prose, and that prose is what a human reads. Rewriting
several hundred call sites to choose a stream would be a large diff across code
paths that need an instrument to exercise -- including `fw flash`. Moving the
stream once, in the dispatcher, changes no call site and cannot change what a
command does.

A command that has something worth reporting adds it:

    from microfreak import jsonout
    ...
    jsonout.result(slot=slot, name=name, bytes=len(payload))

`result()` merges into the pending object and is a no-op when `--json` is not
active, so it is safe to call unconditionally and costs nothing.
"""
import json
import sys

_current = None


def result(**fields):
    """Add fields to the JSON object this command will print.

    A no-op unless --json is active, so call sites need no condition.
    """
    if _current is not None:
        _current.fields.update(fields)


def active():
    """True when --json is in force. For deciding not to compute a report."""
    return _current is not None


class JsonOut:
    """Owns the stream swap and prints the object. Used by cli.run()."""

    def __init__(self, command):
        self.command = command
        self.fields = {}
        self._stdout = sys.stdout

    def __enter__(self):
        global _current
        # Existing prose keeps printing; it just lands on stderr.
        sys.stdout = sys.stderr
        _current = self
        return self

    def __exit__(self, exc_type, exc, tb):
        global _current
        _current = None
        sys.stdout = self._stdout
        body = {"ok": True, "command": self.command, "exit": 0}

        if exc_type is SystemExit:
            code = exc.code
            if isinstance(code, str):
                # sys.exit("message") is how these commands refuse.
                body.update(ok=False, exit=1, error=code)
            else:
                code = 0 if code is None else int(code)
                body.update(ok=(code == 0), exit=code)
        elif exc_type is not None:
            body.update(ok=False, exit=1,
                        error="%s: %s" % (exc_type.__name__, exc))

        body.update(self.fields)
        json.dump(body, self._stdout, indent=1, default=str)
        self._stdout.write("\n")
        self._stdout.flush()
        return False      # never swallow an exception

    def finish(self, returned):
        """Record a command's own return value as the exit code."""
        code = 0 if returned is None else int(returned)
        self.fields.setdefault("exit", code)
        self.fields.setdefault("ok", code == 0)
        return code

"""Ask before a command overwrites something on the instrument."""
import sys

from microfreak import jsonout


def ask(question, yes=False):
    """-> True to go ahead: `yes` was given, or the user answered y.

    Asks only in a terminal. Under --json, or with no terminal to answer,
    it does not ask and the command stays a dry run."""
    if yes:
        return True
    sys.stdout.flush()
    if jsonout.active() or not sys.stdin.isatty():
        print("\ndry run. Add --yes to write.", file=sys.stderr)
        return False
    sys.stderr.write("\n%s [y/N] " % question)
    sys.stderr.flush()
    try:
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        answer = ""
    if answer.strip().lower() in ("y", "yes"):
        return True
    print("nothing written", file=sys.stderr)
    return False

"""Panel numbers in, slot indices out.

The instrument has presets 1-512, samples 1-128 and wavetables 1-16. Those are
the numbers on its screen, the numbers in MCC, and the numbers an owner says
out loud. They are what this tool takes and what it prints.

0-based indices are a wire detail. `divmod(slot, 128)` gives a MIDI bank and
program, and program change is 0-based, so the inside of this package counts
from 0 and always will. Nothing a person types or reads does.

Every command converts here, immediately after `parse_args`, and assigns the
result back over the parsed value. Everything below the CLI keeps its existing
0-based contract untouched -- that is the point of converting at the boundary
rather than threading a second convention through the package. `--json` still
reports both: `slot` is the 0-based index, `panel` is the number on the screen.

This used to be the other way round. The CLI took 0-based slots, so
`mfreak preset write 179` meant the preset the screen calls 180, and every
command's --help, every range error and the README all spoke in a number the
instrument never displays. `sample write`'s help conceded the problem in a
parenthetical -- "0-127 (MCC UI shows 1-128)" -- and `preset list` printed a
`panel` column next to a `slot` column because neither was authoritative.
"""

#: What the screen calls the last one of each kind.
PRESETS, SAMPLES, WAVETABLES = 512, 128, 16


def to_slot(ap, value, count, what="preset"):
    """A 1-based panel number -> the 0-based slot index. Exits 2 if invalid.

    `ap` is the parser, so a bad number is an argparse usage error (exit 2)
    rather than a traceback. `None` passes through, for optional arguments.
    """
    if value is None:
        return None
    if value == 0:
        ap.error(
            "there is no %s 0. Use the number the instrument shows, "
            "starting at 1." % what)
    if not 1 <= value <= count:
        ap.error("%s must be 1-%d, the number the instrument shows. Got %d."
                 % (what, count, value))
    return value - 1

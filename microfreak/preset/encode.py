#!/usr/bin/env python3
"""Build a preset by changing named fields of a template preset.

    mfreak preset encode --template-preset 8 --set filter_cutoff=40 --out new.mfpz
    mfreak preset encode --template-file init.mfpz --params-file p.json --out new.mfpz
    mfreak preset write 9 --file new.mfpz --yes

Every byte the fields do not cover is kept from the template, and so are
its name and category unless --name gives a new name.

Fields:
    knobs, 0-100:   glide, osc_wave, osc_timbre, osc_shape, filter_cutoff,
                    filter_resonance, cycling_env_rise, cycling_env_fall,
                    cycling_env_hold, cycling_env_amount,
                    cycling_env_rise_shape, cycling_env_fall_shape,
                    arp_seq_rate_free, arp_seq_rate_sync, arp_seq_swing,
                    lfo_rate_free, lfo_rate_sync, envelope_attack,
                    envelope_decay, envelope_sustain, spice
    osc_type:       an engine name, e.g. "V. Analog"
    switches:       filter_type, amp_mod, cycling_env_mode, lfo_shape, arp,
                    seq, arp_seq_mod, arp_seq_sync, lfo_sync, paraphonic,
                    octave. The value is a setting name, e.g. "LPF" or "+2"
    mod matrix:     mod:SOURCE:DEST, -100.0 to +100.0 in steps of 0.1
                    sources: cyc_env, env, lfo, press, key_arp
                    destinations: pitch, wave, timbre, cutoff, assign1,
                    assign2, assign3

The full value lists are in microfreak/preset/fields.py.
"""
import argparse
import json
import sys

import microfreak.preset.fields as PF
from microfreak.midi import open_synth, checksum_free_seq
from microfreak.preset.read import category_of, read_name_record, read_preset, schema_of
from microfreak import jsonout
from microfreak import mccfile
from microfreak import panel


def parse_assignment(spec):
    name, _, value = spec.partition("=")
    if not _:
        raise ValueError(f"--set must be NAME=VALUE, got {spec!r}")
    return name.strip(), value.strip()


def apply_assignment(rows, name, value):
    if name.startswith("mod:"):
        _, source, dest = name.split(":")
        if source not in PF.MOD_SOURCES:
            raise ValueError(f"unknown mod source {source!r}, one of {PF.MOD_SOURCES}")
        if dest not in PF.MOD_DESTINATIONS:
            raise ValueError(f"unknown mod destination {dest!r}, one of {PF.MOD_DESTINATIONS}")
        PF.set_mod_matrix_raw(rows, source, dest, float(value))
        return

    if name in PF.SWITCH:
        field = PF.SWITCH[name]
        raw = PF.switch_raw(value, field["values"])
        PF.set_field_raw(rows, field, raw)
        return

    if name == "osc_type":
        # MUST go through set_osc_engine.  osc_type is a TLV record with a
        # count byte and a split 15-bit value, not a bit-packed control field.
        # This used to do `set_field_raw(rows, field, field["inverse"][value])`,
        # which wrote the engine to the wrong place: the preset read back as
        # the requested engine through _OSC_TYPE_NAMES but was NOT on it, and
        # rendered silent. Found 2026-10-06 after it cost a silent preset on
        # hardware and voided two emulator runs. Verify with PF.osc_engine().
        if value not in PF.ENGINE_VALUES:
            raise ValueError(f"{value!r} is not an engine name; one of "
                             f"{sorted(PF.ENGINE_VALUES)}")
        PF.set_osc_engine(rows, value)
        got = PF.osc_engine(rows)
        if got != value:
            raise ValueError(f"engine did not take: wanted {value!r}, got {got!r}")
        return

    if name in PF.CONTROL:
        field = PF.CONTROL[name]
        raw = PF.display_to_raw(float(value), signed=False)
        PF.set_field_raw(rows, field, raw)
        return

    raise ValueError(f"unknown field {name!r} -- see this script's docstring for valid names")


def _uses_mod_matrix(assignments):
    return any(name.startswith("mod:") for name, _ in assignments)


def _check_expanded_layout(template_payload):
    """No longer a constraint.

    Mod-matrix cells used to be addressed by fixed byte positions
    (preset_fields.MOD_MATRIX_WIRE), which are only valid for presets in one
    particular internal layout -- hence the old requirement for a template
    with a connection already saved. Cells are now addressed by name through
    the TLV stream, which works on every preset. Kept as a no-op so callers
    and tests do not break.
    """
    return


def build_payload(template_payload, assignments):
    """-> a patched 4672-byte payload. Pure: the template goes in, bytes come out.

    Every byte not named by an assignment is carried over from the template
    untouched, including the structural bytes nothing here understands yet.
    """
    if _uses_mod_matrix(assignments):
        _check_expanded_layout(template_payload)
    rows = PF.rows_from_payload(template_payload)
    for name, value in assignments:
        apply_assignment(rows, name, value)
    return PF.payload_from_rows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--template-preset", dest="template_slot", type=int,
                      metavar="PRESET",
                      help="read the template from this preset (1-512)")
    src.add_argument("--template-file",
                      metavar="FILE.mfpz", help="an MCC preset file")
    params = ap.add_mutually_exclusive_group()
    params.add_argument("--set", action="append", default=[],
                         help="NAME=VALUE. Repeatable")
    params.add_argument("--params-file",
                         help="a JSON file of {\"name\": value} pairs")
    ap.add_argument("--out", required=True, metavar="FILE.mfpz",
                    help="the MCC preset file to write")
    ap.add_argument("--name", help="name the new preset (default: the template's)")
    ap.add_argument("--port", default="MicroFreak", help="MIDI port name substring (default: MicroFreak)")
    ap.add_argument("--timeout", type=float, default=6.0, help="seconds to wait per reply (default: 6)")
    args = ap.parse_args()

    args.template_slot = panel.to_slot(ap, args.template_slot,
                                       panel.PRESETS, "--template-preset")

    assignments = [parse_assignment(s) for s in args.set]
    if args.params_file:
        with open(args.params_file) as fh:
            for field, value in json.load(fh).items():
                assignments.append((field, value))

    if not assignments:
        ap.error("no assignments given -- use --set NAME=VALUE or --params-file")
    try:
        mccfile.check_ext(args.out, "preset")
        if args.name is not None:
            mccfile.check_name(args.name, "preset")
        if args.template_file:
            template = mccfile.read_one(args.template_file, "preset")
            name, category, template_payload = mccfile.preset_of(template)
            schema = template.archive.schema
    except mccfile.FileError as exc:
        sys.exit(str(exc))

    if not args.template_file:
        synth = open_synth(args.port, timeout=args.timeout)
        seqgen = checksum_free_seq()
        try:
            print(f"reading template from preset {args.template_slot + 1}...",
                  file=sys.stderr)
            record = read_name_record(synth, args.template_slot, seqgen)
            category, schema = category_of(record), schema_of(record)
            name, template_payload = read_preset(synth, args.template_slot, seqgen, verbose=True)
            print(f"  template name: {name!r}", file=sys.stderr)
        finally:
            synth.close()

    print(f"applying {len(assignments)} assignment(s):", file=sys.stderr)
    for field, value in assignments:
        print(f"  {field} = {value}", file=sys.stderr)

    payload = build_payload(template_payload, assignments)

    name = args.name if args.name is not None else name
    mccfile.write_one(args.out, mccfile.preset_archive(name, category, payload, schema),
                      "preset")
    print(f"wrote {args.out}: {name!r}")
    jsonout.result(out=args.out, name=name, bytes=len(payload),
                   assignments=[{"field": n, "value": v}
                                for n, v in assignments],
                   next="mfreak preset write PRESET --file %s --yes" % args.out)
    print(f"next: mfreak preset write PRESET --file {args.out} --yes")


if __name__ == "__main__":
    main()

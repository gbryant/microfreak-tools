#!/usr/bin/env python3
"""Look inside a firmware file, take it apart, or put one together.

    mfreak fw info update.mff
    mfreak fw unpack update.mff --out dir
    mfreak fw repack update.mff --replace main=custom.bin --out custom.mff
    mfreak fw repack update.mff --only main --out main-only.mff

Offline. A file is a .mff, or one image from inside one.
"""

# ## The format
#
# A plain ZIP, every member **Stored** (uncompressed): the five firmware images
# plus `info.json`, which maps `image_num` -> `file_name`.  There is **no Boost
# archive** -- that is the `.mfpz`/`.mfsz`/`.mfwz` preset/sample/wavetable family
# (`RELATED_PROJECTS.md`).  The per-image `.bin` payloads carry Arturia's own 64-byte
# header, documented in microfreak-emu, and ``image.package()`` builds.
#
# Byte-identity needs more than the member bytes, so `selftest` checks it:
#
#   * member **order** is as shipped, with `info.json` last;
#   * every member Stored, with a zero-length extra field;
#   * "version made by" is `0x0014` -- create_system **0 (MS-DOS)**, not the 3
#     (Unix) that `zipfile` would write on this machine;
#   * external attributes `0x81b60000`, internal `0`;
#   * no archive comment, no data descriptors, no zip64 extras.
#
# Replacing a member keeps its name, so `info.json` needs no edit and the rebuilt
# container differs from the shipped one only inside that member.

import argparse, io, json, os, sys, zipfile

from microfreak.firmware import image as I


def read(path):
    """-> (list of (ZipInfo, bytes), archive comment)."""
    with zipfile.ZipFile(path) as z:
        return [(i, z.read(i.filename)) for i in z.infolist()], z.comment


def write(path, members, comment=b""):
    """Rebuild a container, preserving every field `selftest` checks."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for src, data in members:
            zi = zipfile.ZipInfo(src.filename, date_time=src.date_time)
            zi.compress_type = zipfile.ZIP_STORED
            zi.create_system = src.create_system
            zi.create_version = src.create_version
            zi.extract_version = src.extract_version
            zi.internal_attr = src.internal_attr
            zi.external_attr = src.external_attr
            zi.flag_bits = src.flag_bits
            zi.extra = src.extra
            z.writestr(zi, data)
        z.comment = comment
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())


def identify(code):
    """-> one line on what the main image's code is."""
    from microfreak.firmware.patching import identify
    return identify(code)


def _words(code):
    """Print the speech word banks of a build `fw patch` knows."""
    from microfreak.firmware import patching as X
    if not X.known_build(code):
        return
    print("  speech words:")
    for line in X.describe_words(code):
        print("    " + line)


def info(path):
    data = open(path, "rb").read()
    print(path)
    if data[:2] == b"PK":
        meta, images = I.read(path)
        print("  firmware %s, dated %s" % (meta.get("version_number", "?"),
                                          meta.get("date", "?")))
    else:
        found = I.looks_packaged(data)
        if found is None and any(data[2:I.HEADER]):
            print("  the main image's code alone, %d bytes" % len(data))
            print("  main image: %s" % identify(data))
            _words(data)
            return 0
        images = [(found[0] if found else -1, os.path.basename(path), data)]
    bad = 0
    for num, name, blob in images:
        ok = num == I.WAVETABLES or I.looks_packaged(blob) is not None
        bad += not ok
        print("  %-10s %-24s %8d bytes  %-18s %s"
              % (I.NAMES.get(num, "?"), I.LABELS.get(num, "unknown image"),
                 len(blob), "checksum ok" if ok else "CHECKSUM MISMATCH", name))
    main = [blob for num, _, blob in images if num == 0]
    if main and I.looks_packaged(main[0]):
        code = main[0][I.HEADER:-1]
        print("  main image: %s" % identify(code))
        _words(code)
    return 1 if bad else 0


def unpack(path, outdir, code=False):
    members, _ = read(path)
    os.makedirs(outdir, exist_ok=True)
    root = os.path.realpath(outdir)
    for i, data in members:
        # A .mff is a zip, and a zip can name a member `../x` or an absolute
        # path. Arturia's are flat file names, so nothing real is refused.
        p = os.path.realpath(os.path.join(root, i.filename))
        if p != root and not p.startswith(root + os.sep):
            sys.exit("refusing to unpack %r: it resolves outside %s"
                     % (i.filename, outdir))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "wb").write(data)
        print("  %-52s %8d bytes" % (i.filename, len(data)))
    if code:
        p = os.path.join(root, "main.raw")
        open(p, "wb").write(I.main_code(path))
        print("  %-52s %8d bytes" % ("main.raw", os.path.getsize(p)))
    print("-> %s" % outdir)
    return 0


def as_image(path, num):
    """-> the bytes to put in a .mff for image `num`, from a packaged image
    or, for any image but the wavetables, its body alone."""
    data = open(path, "rb").read()
    if num == I.WAVETABLES:
        return data
    found = I.looks_packaged(data)
    if found is None:
        return I.package(data, num)
    if found[0] != num:
        sys.exit("%s is the %s image, not the %s one"
                 % (path, I.LABELS.get(found[0], found[0]), I.LABELS[num]))
    return data


def repack(path, out, replace, only):
    subs = {}
    for spec in replace:
        name, _, file = spec.partition("=")
        if not file:
            sys.exit("--replace wants IMAGE=FILE, got %r" % spec)
        try:
            num = I.image_number(name)
        except ValueError:
            sys.exit("--replace: %r is not an image. Use one of: %s"
                     % (name, ", ".join(I.NAMES.values())))
        subs[num] = as_image(file, num)

    if open(path, "rb").read(2) != b"PK":
        found = I.looks_packaged(open(path, "rb").read())
        if found is None:
            sys.exit("%s is not a .mff or a packaged image" % path)
        images = [(found[0], os.path.basename(path), open(path, "rb").read())]
        images = [(n, name, subs.get(n, blob)) for n, name, blob in images
                  if not only or n in only]
        if not images:
            sys.exit("--only leaves nothing to write")
        I.write(out, images)
    else:
        meta, images = I.read(path)
        have = {n for n, _, _ in images}
        for n in list(subs) + list(only or []):
            if n not in have:
                sys.exit("%s has no %s image" % (path, I.NAMES[n]))
        keep = {name: n for n, name, _ in images if not only or n in only}
        members, comment = read(path)
        rebuilt = []
        for zi, data in members:
            if zi.filename == "info.json":
                if only:
                    meta = dict(meta, images=[i for i in meta["images"]
                                              if i["file_name"] in keep])
                    data = json.dumps(meta, indent=4).encode()
            elif zi.filename in keep:
                data = subs.get(keep[zi.filename], data)
            else:
                continue
            rebuilt.append((zi, data))
        # info.json last, as Arturia ship it
        rebuilt.sort(key=lambda m: m[0].filename == "info.json")
        write(out, rebuilt, comment)

    _, back = I.read(out)
    for num, name, blob in back:
        ok = num == I.WAVETABLES or I.looks_packaged(blob) is not None
        print("  %-10s %8d bytes  %s%s"
              % (I.NAMES.get(num, num), len(blob), name,
                 "  replaced" if num in subs else ""))
        if not ok:
            sys.exit("%s: the %s image's checksum does not match" % (out, name))
    print("-> %s, checked" % out)
    return 0


def main():
    RAW = argparse.RawDescriptionHelpFormatter
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=RAW)
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser(
        "info", formatter_class=RAW,
        description="Show what is in a firmware file: its version, each "
                    "image and its checksum,\nand whether the main image is "
                    "stock or patched. Offline.",
        epilog="    mfreak fw info MicroFreak_Firmware_Update_5_0_0_2084.mff")
    i.add_argument("file", metavar="FILE",
                   help="a .mff, or one image from inside one")
    u = sub.add_parser(
        "unpack", formatter_class=RAW,
        description="Extract every image and info.json from a .mff. Offline.",
        epilog="    mfreak fw unpack update.mff --out update/")
    u.add_argument("file", metavar="FILE.mff", help="the firmware file")
    u.add_argument("--out", "-o", dest="outdir", default=".", metavar="DIR",
                   help="where to extract (default: here)")
    u.add_argument("--code", action="store_true",
                   help="also write main.raw, the main image's code alone, "
                        "for a disassembler")
    r = sub.add_parser(
        "repack", formatter_class=RAW,
        description="Write a new .mff from an existing one, with images "
                    "replaced or left out.\nOffline.",
        epilog="    mfreak fw repack update.mff --replace main=custom.bin "
               "--out custom.mff\n"
               "    mfreak fw repack update.mff --only main --out main.mff\n"
               "    mfreak fw repack custom.bin --out custom.mff\n\n"
               "A replacement can be a packaged image or its body alone; the "
               "header and\nchecksum are added. Images: %s."
               % ", ".join(I.NAMES.values()))
    r.add_argument("file", metavar="FILE",
                   help="a .mff, or one packaged image to put in a .mff")
    r.add_argument("--replace", action="append", default=[],
                   metavar="IMAGE=FILE", help="replace an image. Repeatable")
    r.add_argument("--only", type=I.image_number_arg, action="append",
                   default=None, metavar="IMAGE",
                   help="keep only this image. Repeatable")
    r.add_argument("--out", required=True, metavar="NEW.mff",
                   help="the .mff to write")
    a = ap.parse_args()
    if a.cmd == "info":
        return info(a.file)
    if a.cmd == "unpack":
        return unpack(a.file, a.outdir, a.code)
    if os.path.abspath(a.out) == os.path.abspath(a.file):
        sys.exit("--out must be a new file, not %s" % a.file)
    return repack(a.file, a.out, a.replace, a.only)


if __name__ == "__main__":
    sys.exit(main())

"""Firmware files: the .mff Arturia ship, the images inside it, and the main
image's code.

A .mff is a ZIP holding up to five images and `info.json`, which maps each
`image_num` to its member. Every image but the wavetable bank is packaged: a
0x40-byte header (image number, XOR of the body, then zeros), the body, and
the XOR again. The main image's body is the application code, loaded at
0x0800C000, which is what `fw patch` changes.
"""
import json
import sys
import time
import zipfile

HEADER = 0x40

# Arturia's numbering, from info.json. The short names are what --image and
# --target take.
NAMES = {0: "main", 1: "oled", 2: "matrix", 3: "keyboard", 4: "wavetables"}
LABELS = {0: "main application", 1: "OLED co-processor",
          2: "matrix co-processor", 3: "keyboard co-processor",
          4: "factory wavetable bank"}
WAVETABLES = 4


def image_number(text):
    """'main' or '0' -> 0. For argparse `type=`."""
    for num, name in NAMES.items():
        if text == name or text == str(num):
            return num
    raise ValueError(text)


def image_number_arg(text):
    import argparse
    try:
        return image_number(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "%r is not an image. Use one of: %s"
            % (text, ", ".join(NAMES.values())))


def looks_packaged(data):
    """-> (image number, xor) if `data` is a packaged image, else None."""
    if len(data) < HEADER + 2 or any(data[2:HEADER]):
        return None
    xor = 0
    for b in data[HEADER:-1]:
        xor ^= b
    return (data[0], xor) if xor == data[1] == data[-1] else None


def package(body, num=0):
    """-> `body` with Arturia's header and trailing XOR."""
    xor = 0
    for b in body:
        xor ^= b
    out = bytearray(HEADER)
    out[0], out[1] = num, xor
    return bytes(out + bytes(body) + bytes([xor]))


def read(path):
    """-> (info, [(image number, member name, bytes)]) for a .mff, in image
    order. Exits with a message for anything else."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError as e:
        sys.exit("%s: %s" % (path, e.strerror or e))
    if head[:2] != b"PK":
        sys.exit("%s is not a .mff firmware file." % path)
    with zipfile.ZipFile(path) as z:
        try:
            info = json.loads(z.read("info.json"))
        except KeyError:
            sys.exit("%s is not a .mff firmware file (no info.json)." % path)
        names = set(z.namelist())
        out = []
        for i in info.get("images", []):
            if i["file_name"] not in names:
                sys.exit("%s: info.json names %s, which is not in the file"
                         % (path, i["file_name"]))
            out.append((int(i["image_num"]), i["file_name"],
                        z.read(i["file_name"])))
    out.sort(key=lambda r: r[0])
    return info, out


def write(path, images, version="custom", date=None):
    """Write [(image number, member name, packaged bytes)] as a .mff."""
    info = {"version_number": version,
            "date": date or time.strftime("%Y_%m_%d"),
            "images": [{"image_num": n, "file_name": name}
                       for n, name, _ in images]}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for _, name, blob in images:
            z.writestr(name, blob)
        z.writestr("info.json", json.dumps(info, indent=4))


def main_code(path):
    """-> the main application's code from a .mff, a packaged main image, or
    the code alone. The checksum is checked when there is one."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as e:
        sys.exit("%s: %s" % (path, e.strerror or e))
    if data[:2] == b"PK":
        _, images = read(path)
        main = [blob for num, _, blob in images if num == 0]
        if not main:
            sys.exit("%s has no main image" % path)
        data = main[0]
        if looks_packaged(data) is None:
            sys.exit("%s: the main image's checksum does not match" % path)
    if any(data[2:HEADER]):
        return bytearray(data)          # no header: already the code
    found = looks_packaged(data)
    if found is None:
        sys.exit("%s: the checksum does not match" % path)
    if found[0] != 0:
        sys.exit("%s is the %s image, not the main one"
                 % (path, LABELS.get(found[0], "image %d" % found[0])))
    return bytearray(data[HEADER:-1])

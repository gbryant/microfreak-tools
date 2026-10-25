#!/usr/bin/env python3
"""Flash a MicroFreak over USB DFU, without MIDI Control Center.

    mfreak fw flash update.mff --plan        what would be sent, no device
    mfreak fw probe                          read-only: who is on the bus
    mfreak fw enter                          reboot into the bootloader
    mfreak fw flash update.mff               the real thing

The protocol is plain USB DFU with three Arturia extensions, mapped from
`captures/firmware-update.pcapng` (see `FIRMWARE_UPDATE.md`). `plan()` builds
the exact request sequence `flash` sends, every request and payload byte,
without a device, and that sequence was checked against a capture of MCC
performing a real update.

## The protocol, in nine request kinds

| request | bmRT | bReq | |
|---|---|---|---|
| `ARTURIA_REBOOT_TO_DFU` | `0x21` | `0xF2` | the running app marks flash and resets |
| `ARTURIA_SELECT_TARGET` | `0x21` | `0xF4` | `wValue` picks one of five images |
| `ARTURIA_FINISH`        | `0x21` | `0xF3` | validate and boot the application |
| `DFU_DNLOAD`            | `0x21` | `0x01` | `wValue` = block number, 64 bytes |
| `DFU_GETSTATUS`         | `0xA1` | `0x03` | 6 bytes; polled twice per block |
| `DFU_GETSTATE`          | `0xA1` | `0x05` | 1 byte |

plus `SET_INTERFACE`, and that is all of it. `DFU_DETACH`, `DFU_UPLOAD`,
`DFU_CLRSTATUS` and `DFU_ABORT` never appear -- there is no read-back of
installed firmware in this protocol, so a tool cannot verify what it wrote.

## Recovery, if an update is interrupted

**Hold SHIFT + ARP + OCT/MOD while powering the instrument on.** The utility
light comes on and it is in DFU, ready to re-flash. The bootloader samples one
GPIO (`GPIOB->IDR` bit 10) before it looks at flash at all, so this works
whatever a failed update left behind. Two further gates mean it will not boot a
broken image either: a per-image validity marker in flash sector 2, and a check
that the application's reset vector has top byte `0x08`. See `BOOTLOADER.md`.

## Three things that make this safer than it sounds

**The payloads go out verbatim.** Each `.bin` member of the `.mff` is sent as
it sits, Arturia's own 64-byte header included, in 64-byte blocks -- block 0 is
that header. Nothing is recomputed host-side, so there is no checksum or
layout logic here to get wrong. `info.json` maps `image_num` to file name, so
the target numbers are read from the container rather than hardcoded.

**It sends what the file holds.** Image 0 is the application, 1-3 are the
co-processors and 4 is the factory wavetable bank. A stock .mff holds all five
and MCC always sends all five, so `flash` does too. `fw patch` copies the
other four from the stock .mff it patches, so a patched file is complete as
well. `--only` picks images out of a file.
"""
import argparse
import os
import sys
import time

from microfreak import confirm
from microfreak.firmware import image as I

VENDOR, PRODUCT = 0x1C75, 0x0601

# How to tell the bootloader from the application. **Not bcdDevice** -- that is
# 0x0200 in both, and an earlier reading of this capture said otherwise. What
# changes is bcdUSB (0x0200 -> 0x0201), which is a strange thing to vary and a
# poor thing to depend on. The number of interfaces is the real signal and it
# means something: the application exposes DFU + AudioControl + MIDIStreaming
# at 250 mA, the bootloader exposes DFU alone at 400 mA.
APP_INTERFACES, BOOTLOADER_INTERFACES = 3, 1


def in_bootloader(dev):
    """-> True if this is the DFU-only bootloader configuration."""
    return dev[0].bNumInterfaces == BOOTLOADER_INTERFACES

# bmRequestType, bRequest
OUT_IFACE, IN_IFACE = 0x21, 0xA1
DFU_DNLOAD, DFU_GETSTATUS, DFU_GETSTATE = 0x01, 0x03, 0x05
ARTURIA_REBOOT, ARTURIA_FINISH, ARTURIA_SELECT = 0xF2, 0xF3, 0xF4

TRANSFER_SIZE = 64              # wTransferSize from the DFU functional descriptor

# Per-block intervals from MCC's capture, in seconds. Used ONLY to print a
# pessimistic estimate before a run; nothing waits on them.
#
# They turned out to measure the capture host, not the protocol. MCC inserts no
# delay (0.20 ms of target 0's 10.00 ms is host idle; the rest is inside the
# transfers), but neither is it device time: this tool wrote the same image to
# the same instrument in ~6 s, ~3.8 s of which is the erase, so the download ran
# ~30x faster than the capture. USBPcap timestamps in a kernel filter driver in
# the URB path, so "inside the transfer" there includes the Windows stack and
# the capture itself. Two readings of this were wrong in opposite directions
# before an uncaptured host settled it.
BLOCK_INTERVAL = {0: 0.0100, 1: 0.0300, 2: 0.0300, 3: 0.0300, 4: 0.0106}

# Control-transfer timeouts, in milliseconds. libusb defaults to 1000, which is
# not enough: ARTURIA_SELECT_TARGET performs the flash ERASE inside the control
# transfer and took **3775 ms** for target 0 in the capture (690 ms for the
# co-processors, 212 ms for the wavetable bank). The first attempt at flashing
# died exactly there. The erase is not a gap before block 0, as the timing
# table first described it -- it is the request's own duration.
TIMEOUT_SELECT = 20000          # >5x the observed worst case
TIMEOUT_FINISH = 20000          # validates and boots; measured 3 ms, but it resets
TIMEOUT_BLOCK = 5000            # DNLOAD 4 ms, GETSTATUS up to 56 ms observed

DFU_STATUS = {0: "OK", 1: "errTARGET", 2: "errFILE", 3: "errWRITE", 4: "errERASE",
              5: "errCHECK_ERASED", 6: "errPROG", 7: "errVERIFY", 8: "errADDRESS",
              9: "errNOTDONE", 10: "errFIRMWARE", 11: "errVENDOR", 12: "errUSBR",
              13: "errPOR", 14: "errUNKNOWN", 15: "errSTALLEDPKT"}
DFU_STATE = {0: "appIDLE", 1: "appDETACH", 2: "dfuIDLE", 3: "dfuDNLOAD-SYNC",
             4: "dfuDNBUSY", 5: "dfuDNLOAD-IDLE", 6: "dfuMANIFEST-SYNC",
             7: "dfuMANIFEST", 8: "dfuMANIFEST-WAIT-RESET", 9: "dfuUPLOAD-IDLE",
             10: "dfuERROR"}


def images(path):
    """-> [(image_num, file_name, bytes)] in the order MCC sends them."""
    if os.path.isfile(path) and open(path, "rb").read(2) != b"PK":
        found = I.looks_packaged(open(path, "rb").read())
        if found:
            sys.exit("%s is one image, not a .mff. `mfreak fw repack %s "
                     "--out NEW.mff` puts it in one." % (path, path))
    return I.read(path)[1]


def plan(path, targets=None):
    """-> the full request sequence, as (label, bmRT, bReq, wValue, data).

    Pure: no device, no USB. This is what was checked against the capture
    and what `flash` walks. `data` is None for a request with no data stage.
    """
    seq = [("ARTURIA_REBOOT_TO_DFU", OUT_IFACE, ARTURIA_REBOOT, 0, None)]
    for num, name, blob in images(path):
        if targets is not None and num not in targets:
            continue
        seq.append(("SET_INTERFACE", 0x01, 0x0B, 0, None))
        seq.append(("DFU_GETSTATE", IN_IFACE, DFU_GETSTATE, 0, None))
        seq.append(("ARTURIA_SELECT_TARGET %d (%s)" % (num, name),
                    OUT_IFACE, ARTURIA_SELECT, num, None))
        for i in range(0, len(blob), TRANSFER_SIZE):
            block = i // TRANSFER_SIZE
            seq.append(("DFU_DNLOAD block %d" % block, OUT_IFACE, DFU_DNLOAD,
                        block, blob[i:i + TRANSFER_SIZE]))
            seq.append(("DFU_GETSTATUS", IN_IFACE, DFU_GETSTATUS, 0, None))
            seq.append(("DFU_GETSTATUS", IN_IFACE, DFU_GETSTATUS, 0, None))
        end = len(blob) // TRANSFER_SIZE + (1 if len(blob) % TRANSFER_SIZE else 0)
        seq.append(("DFU_DNLOAD end-of-transfer", OUT_IFACE, DFU_DNLOAD, end, b""))
        seq.append(("DFU_GETSTATUS", IN_IFACE, DFU_GETSTATUS, 0, None))
    seq.append(("ARTURIA_FINISH", OUT_IFACE, ARTURIA_FINISH, 0, None))
    return seq


# The requests the OS issues during enumeration, which a flashing tool does not
# send and the capture comparison therefore ignores: GET_DESCRIPTOR and
# SET_CONFIGURATION. (Named `verify` after the subcommand that never shipped.)
ENUMERATION = {(0x80, 0x06), (0x00, 0x09)}


# --- the USB side ----------------------------------------------------------
#
# pyusb + libusb. On macOS no kernel driver claims interface 0 (class 0xFE,
# application-specific), so it can be claimed while AppleUSBAudio holds the
# audio and MIDI interfaces -- no kext unloading, no SIP changes. In the
# bootloader there is only the DFU interface, so the question does not arise.
# `probe` is what establishes this on a given machine, and it writes nothing.

def _usb():
    try:
        import usb.core
        import usb.util
    except ImportError:
        sys.exit("needs pyusb:\n"
                 "    uv add pyusb          (or: .venv/bin/pip install pyusb)\n"
                 "`fw flash --plan` works without it.")
    return usb.core, usb.util


_BACKEND = []


def backend():
    """-> the libusb backend to hand pyusb, or None to let it search.

    `libusb-package` ships libusb itself as a platform wheel, so the usual
    case needs no `brew install libusb` -- which was the likeliest way for
    `fw flash` to fail on a machine that was otherwise fine, and it failed
    looking like a broken synth rather than a missing library.

    None is a working answer, not an error: pyusb then searches for a system
    libusb exactly as before. That matters on a platform libusb-package has
    no wheel for, where it is not installed at all.
    """
    if not _BACKEND:
        try:
            import libusb_package
            _BACKEND.append(libusb_package.get_libusb1_backend())
        except Exception:
            # Either not installed, or a bundled library that will not load.
            # Neither is a reason to give up while a system one may be there.
            _BACKEND.append(None)
    return _BACKEND[0]


def backend_label():
    """-> where the libusb in use came from, for `doctor` to report."""
    be = backend()
    if be is None:
        return "system libusb (libusb-package not installed)"
    lib = getattr(getattr(be, "lib", None), "_name", "") or ""
    if "libusb_package" in str(lib):
        return "bundled with libusb-package -- no system libusb needed"
    return "system libusb (%s)" % (os.path.basename(str(lib)) or "found")


def find(required=True):
    """-> the MicroFreak, or None. Matches VID/PID, either mode."""
    core, _ = _usb()
    dev = core.find(idVendor=VENDOR, idProduct=PRODUCT, backend=backend())
    if dev is None and required:
        sys.exit("no MicroFreak on USB (looked for %04X:%04X). Check it is "
                 "on and connected by USB, not only MIDI DIN."
                 % (VENDOR, PRODUCT))
    return dev


def dfu_interface(dev):
    """-> (interface number, alt setting) of the DFU interface, or None.

    Found by class, not by number: FE/01/02 is application-specific / DFU /
    DFU-mode. Both the application's 3-interface configuration and the
    bootloader's 1-interface one expose it, which is what makes `0xF2`
    reachable from a running instrument.
    """
    for cfg in dev:
        for intf in cfg:
            if (intf.bInterfaceClass, intf.bInterfaceSubClass) == (0xFE, 0x01):
                return intf.bInterfaceNumber, intf.bAlternateSetting
    return None


def get_status(dev, intf):
    """-> (bStatus, bwPollTimeout_ms, bState). Raises on a short reply."""
    r = dev.ctrl_transfer(IN_IFACE, DFU_GETSTATUS, 0, intf, 6, TIMEOUT_BLOCK)
    if len(r) != 6:
        raise IOError("DFU_GETSTATUS returned %d bytes, expected 6" % len(r))
    return r[0], r[1] | (r[2] << 8) | (r[3] << 16), r[4]


def describe_status(st):
    status, poll, state = st
    return "%s / %s%s" % (DFU_STATUS.get(status, hex(status)),
                          DFU_STATE.get(state, hex(state)),
                          "" if poll == 0 else "  poll %d ms" % poll)


def cmd_probe(args):
    """Read-only. Nothing is written, no state is changed."""
    core, util = _usb()
    dev = find()
    print("found %04X:%04X  bcdUSB %04X  bcdDevice %04X  bus %s address %s"
          % (dev.idVendor, dev.idProduct, dev.bcdUSB, dev.bcdDevice,
             dev.bus, dev.address))
    try:
        print("  manufacturer : %s" % util.get_string(dev, dev.iManufacturer))
        print("  product      : %s" % util.get_string(dev, dev.iProduct))
        print("  serial       : %s" % util.get_string(dev, dev.iSerialNumber))
    except Exception as e:
        print("  (string descriptors unreadable: %s)" % e)
    for cfg in dev:
        print("  configuration %d: %d interfaces, %d mA"
              % (cfg.bConfigurationValue, cfg.bNumInterfaces, cfg.bMaxPower * 2))
        for intf in cfg:
            tag = ""
            if (intf.bInterfaceClass, intf.bInterfaceSubClass) == (0xFE, 0x01):
                tag = "  <- DFU"
            print("    interface %d alt %d  class %02X/%02X/%02X  %d endpoints%s"
                  % (intf.bInterfaceNumber, intf.bAlternateSetting,
                     intf.bInterfaceClass, intf.bInterfaceSubClass,
                     intf.bInterfaceProtocol, intf.bNumEndpoints, tag))
    found = dfu_interface(dev)
    if found is None:
        sys.exit("\nno DFU interface -- this is not the device this tool knows")
    intf, alt = found
    mode = "bootloader (DFU only)" if in_bootloader(dev) else "application"
    print("\nDFU interface %d, and the device is running the %s" % (intf, mode))
    try:
        util.claim_interface(dev, intf)
    except Exception as e:
        print("could not claim interface %d: %s" % (intf, e))
        print("Another program may be holding it. Quit MIDI Control Center "
              "and try again.")
        return 1
    print("claimed interface %d" % intf)

    rc = 0
    try:
        if in_bootloader(dev):
            state = dev.ctrl_transfer(IN_IFACE, DFU_GETSTATE, 0, intf, 1)
            print("DFU_GETSTATE : %s" % DFU_STATE.get(state[0], hex(state[0])))
            print("DFU_GETSTATUS: %s" % describe_status(get_status(dev, intf)))
            print("\nready for `fw flash`")
        else:
            rc = probe_application(dev, intf)
    finally:
        util.release_interface(dev, intf)
    if not in_bootloader(dev):
        from microfreak.doctor import firmware_version
        print("\nMIDI: %s" % (firmware_version()
                               or "no answer to an identity request"))
    print("\nread-only probe complete -- nothing was written")
    return rc


def probe_application(dev, intf):
    """Confirm the running app's DFU handler is the one we mapped. Read-only.

    `usbd_class_setup` at 0x0805756C answers exactly four class requests --
    DNLOAD (0x01), 0x81, REBOOT (0xF2) -- accepts 0xF3-0xF5 as no-ops, and
    stalls everything else. So in application mode:

        DFU_GETSTATUS (0x03)  must STALL      <- it is not implemented here
        class 0xF5            must be ACCEPTED, doing nothing

    That pair is a positive and a negative control for the decompile, and both
    are read-only: 0xF5 falls past the 0xF2 test and past the `> 0xF5` stall
    and simply returns. MCC agrees about the first one -- all 29,384 of its
    status queries come after the reboot, never before.
    """
    import usb.core
    ok = True

    try:
        get_status(dev, intf)
        print("DFU_GETSTATUS: answered, which the application should not do")
        ok = False
    except usb.core.USBError:
        print("DFU_GETSTATUS: stalled, as the application should")

    try:
        dev.ctrl_transfer(OUT_IFACE, 0xF5, 0, intf, None)
        print("class 0xF5   : accepted as a no-op, as the application should")
    except usb.core.USBError as e:
        print("class 0xF5   : stalled (%s), which the application should "
              "not do" % e)
        ok = False

    if ok:
        print("\nThe DFU handler matches the one mapped in FIRMWARE_UPDATE.md.")
        print("`fw enter` would be answered.")
    return 0 if ok else 1


def reboot_to_bootloader(dev, util, timeout=20.0):
    """ARTURIA_REBOOT_TO_DFU, then wait for the DFU-only configuration.

    -> the re-enumerated device, or None on timeout. Shared by `enter` and
    `flash --enter` so there is one copy of the wait.
    """
    found = dfu_interface(dev)
    if found is None:
        sys.exit("no DFU interface on this device")
    intf, _ = found
    util.claim_interface(dev, intf)
    print("sending ARTURIA_REBOOT_TO_DFU (bmRT 0x21, bRequest 0xF2)")
    try:
        dev.ctrl_transfer(OUT_IFACE, ARTURIA_REBOOT, 0, intf, None)
    except Exception as e:
        # The device may reset before completing the status stage.
        print("  (control transfer reported %s -- expected if it reset early)" % e)
    print("waiting for it to come back as the bootloader")
    for _ in range(int(timeout / 0.5)):
        time.sleep(0.5)
        try:
            again = find(required=False)
        except SystemExit:
            again = None
        if again is not None and in_bootloader(again):
            print("  bootloader up: %d interface, bcdUSB %04X"
                  % (again[0].bNumInterfaces, again.bcdUSB))
            return again
    return None


def cmd_enter(args):
    """Send ARTURIA_REBOOT_TO_DFU and nothing else.

    The app clears image 0's validity marker in flash sector 2, waits 500 ms
    and resets. About 1.3 s later the device is back as a DFU-only device:
    one interface, bcdUSB 0x0201 rather than 0x0200 (bcdDevice is 0x0200 in
    both). The bootloader reads the markers at 0x0800135C and will not boot
    an image whose marker is invalid. See BOOTLOADER.md.

    Recovery from any state: SHIFT + ARP + OCT/MOD at power-on. The
    bootloader samples GPIOB->IDR bit 10 (0x08001328) before reading flash.
    It also checks the reset vector's top byte is 0x08 (0x0800133C), so it
    will not jump into a half-erased image.

    Asks before any USB call: it takes no required arguments, and a test
    sweep once ran it and stranded the instrument.
    """
    print("The screen goes dark, only the utility light stays on, and it has "
          "no MIDI or\naudio until you power-cycle it or flash it. No "
          "firmware is written.")
    if not confirm.ask("Reboot the instrument into its bootloader?", args.yes):
        return 0
    core, util = _usb()
    dev = find()
    if in_bootloader(dev):
        print("already in the bootloader (%d interface); nothing to do"
              % dev[0].bNumInterfaces)
        return 0
    if reboot_to_bootloader(dev, util) is None:
        print("did not see the bootloader within 20 s")
        return 1
    if args.watch:
        print()
        return cmd_watch(argparse.Namespace(seconds=args.watch))
    print("\nIt is in the bootloader. Power-cycle it to use it again.")
    return 0


def cmd_watch(args):
    """Poll the device's mode and report every transition. Read-only.

    Written to answer one question: once `enter` has put the instrument in the
    bootloader, does it STAY there, or does it time out and boot the
    application on its own? Those two look identical if you only check after a
    power cycle, and the difference decides whether `enter` gives you a state
    you can rely on.

    The capture cannot answer it. MCC was continuously busy from 18.56 s to
    242.13 s, so its longest idle in the bootloader was 4.8 s.
    """
    core, util = _usb()
    last, t0 = None, time.time()
    print("watching for %d s -- nothing is written. Ctrl-C to stop." % args.seconds)
    while time.time() - t0 < args.seconds:
        dev = find(required=False)
        if dev is None:
            mode = "absent"
        else:
            n = dev[0].bNumInterfaces
            mode = "bootloader (%d interface)" % n if in_bootloader(dev) \
                else "application (%d interfaces)" % n
        if mode != last:
            print("  %6.1f s  %s" % (time.time() - t0, mode))
            last = mode
        time.sleep(1.0)
    print("  %6.1f s  (end of watch, still %s)" % (time.time() - t0, last))
    return 0


def cmd_flash(args):
    imgs = images(args.mff)
    if args.only:
        missing = [n for n in args.only if n not in {num for num, _, _ in imgs}]
        if missing:
            sys.exit("%s has no %s image. It holds: %s"
                     % (args.mff, I.NAMES.get(missing[0], missing[0]),
                        ", ".join(I.NAMES.get(n, str(n)) for n, _, _ in imgs)))
        imgs = [r for r in imgs if r[0] in args.only]

    # Check every payload's own checksum before sending a byte. The device
    # cannot tell us afterwards -- there is no DFU_UPLOAD in this protocol.
    for num, name, blob in imgs:
        if num == I.WAVETABLES:
            continue                       # carries a 0x7981 magic, not a header
        found = I.looks_packaged(blob)
        if found is None:
            sys.exit("%s: the %s image's checksum does not match. Not "
                     "sending it." % (args.mff, I.LABELS.get(num, num)))
        if found[0] != num:
            sys.exit("%s: %s says image %d inside but info.json says %d"
                     % (args.mff, name, found[0], num))

    if args.plan:
        return cmd_plan(args.mff, {num for num, _, _ in imgs})

    print("%s will write %d image%s:"
          % (os.path.basename(args.mff), len(imgs), "" if len(imgs) == 1 else "s"))
    for num, name, blob in imgs:
        print("  %-10s %-24s %8d bytes  %s"
              % (I.NAMES.get(num, num), I.LABELS.get(num, ""), len(blob), name))
    print("\nThis writes the instrument's flash. If it is interrupted, hold "
          "SHIFT + ARP +\nOCT/MOD while powering on, then flash again.")
    if not confirm.ask("Flash it?", args.yes):
        return 0

    core, util = _usb()
    dev = find()
    if not in_bootloader(dev):
        dev = reboot_to_bootloader(dev, util)
        if dev is None:
            sys.exit("did not see the bootloader within 20 s; nothing written")
    found = dfu_interface(dev)
    if found is None:
        sys.exit("no DFU interface")
    intf, _ = found
    util.claim_interface(dev, intf)

    for num, name, blob in imgs:
        dev.set_interface_altsetting(interface=intf, alternate_setting=0)
        state = dev.ctrl_transfer(IN_IFACE, DFU_GETSTATE, 0, intf, 1)[0]
        print("\ntarget %d %s -- state %s"
              % (num, name, DFU_STATE.get(state, hex(state))))
        # This is the erase, and it runs INSIDE the transfer: 3.8 s for target 0.
        print("  selecting it (this performs the erase -- up to a few seconds)")
        dev.ctrl_transfer(OUT_IFACE, ARTURIA_SELECT, num, intf, None,
                          TIMEOUT_SELECT)
        nblocks = (len(blob) + TRANSFER_SIZE - 1) // TRANSFER_SIZE
        t0 = time.time()
        for block in range(nblocks):
            chunk = blob[block * TRANSFER_SIZE:(block + 1) * TRANSFER_SIZE]
            dev.ctrl_transfer(OUT_IFACE, DFU_DNLOAD, block, intf, chunk,
                              TIMEOUT_BLOCK)
            # No sleep: the device paces this by holding the transfers open.
            for _ in range(2):
                st = get_status(dev, intf)
                if st[0] != 0:
                    sys.exit("\nblock %d: %s. Stopped with the image "
                             "incomplete. Run the flash again before you "
                             "power-cycle."
                             % (block, describe_status(st)))
                if st[1]:
                    time.sleep(st[1] / 1000.0)
            if block % 200 == 0 or block == nblocks - 1:
                pct = 100.0 * (block + 1) / nblocks
                print("\r  block %d/%d  %5.1f%%  %4.0f s"
                      % (block + 1, nblocks, pct, time.time() - t0), end="")
                sys.stdout.flush()
        dev.ctrl_transfer(OUT_IFACE, DFU_DNLOAD, nblocks, intf, b"",
                          TIMEOUT_BLOCK)
        st = get_status(dev, intf)
        print("\n  end-of-transfer: %s" % describe_status(st))
        if st[0] != 0:
            sys.exit("end-of-transfer reported %s" % describe_status(st))

    print("\nsending ARTURIA_FINISH")
    try:
        dev.ctrl_transfer(OUT_IFACE, ARTURIA_FINISH, 0, intf, None,
                          TIMEOUT_FINISH)
    except Exception as e:
        print("  (reported %s; expected if it reset early)" % e)
    print("done. Waiting for it to restart.")
    report_restart()
    return 0


def report_restart(timeout=60.0):
    """Wait for the application to come back, and say what it runs."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(1.0)
        try:
            dev = find(required=False)
        except SystemExit:
            dev = None
        if dev is not None and not in_bootloader(dev):
            break
    else:
        print("it has not come back after %d s. Power-cycle it." % timeout)
        return
    from microfreak.doctor import firmware_version
    version = None
    while version is None and time.time() - t0 < timeout:
        time.sleep(1.0)
        version = firmware_version(wait=1.0, tries=1)
    print("it is back: %s" % (version or "running, but not answering MIDI "
                              "yet. `mfreak doctor` shows its firmware"))


def cmd_plan(path, targets):
    """Print what `flash` would send, by request kind."""
    seq = plan(path, targets)
    counts = {}
    for label, bmrt, brq, wv, data in seq:
        key = label.split(" block")[0].split(" end-of")[0]
        counts[key] = counts.get(key, 0) + 1
    for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("%6d  %s" % (v, k))
    payload = sum(len(d) for _, _, _, _, d in seq if d)
    print("\n%d requests, %d payload bytes" % (len(seq), payload))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    HIDE = argparse.SUPPRESS
    sub.add_parser(
        "probe", help="read-only: what is on the bus",
        description="Show what the instrument reports over USB, which mode "
                    "it is in, and the\nfirmware it runs. Read-only. The "
                    "output includes your serial number; do not\npost it "
                    "publicly.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    e = sub.add_parser(
        "enter",
        help="reboot into the bootloader; writes no firmware",
        description="Reboot the instrument into its bootloader. `fw flash` "
                    "does this itself.\n\n"
                    "    mfreak fw enter\n\n"
                    "No firmware is written, but the instrument is unusable "
                    "until you power-cycle\nit or flash it. It asks first. "
                    "--yes skips the question.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    e.add_argument("--watch", type=int, metavar="SECONDS", default=0,
                   help="then watch the USB bus for this long")
    e.add_argument("--yes", action="store_true",
                   help="reboot without asking")
    w = sub.add_parser(
        "watch", help="read-only: report mode changes over time",
        description="Report whenever the instrument appears, disappears or "
                    "changes mode on USB.\nRead-only. Useful around `fw "
                    "enter` and power cycles.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    w.add_argument("--seconds", type=int, default=180, metavar="N",
                   help="how long to watch for (default: 180)")
    f = sub.add_parser(
        "flash", help="write images",
        description="Install a firmware file on the instrument over USB. "
                    "THIS WRITES FLASH.\n\n"
                    "    mfreak fw flash MicroFreak_Firmware_Update_5_0_0_2084.mff\n"
                    "    mfreak fw flash speech.mff\n"
                    "    mfreak fw flash update.mff --plan       what it would "
                    "send, no instrument\n\n"
                    "It writes every image in the file, as MCC does, after "
                    "listing them and asking.\n--yes skips the question. It "
                    "reboots the instrument into its bootloader,\nflashes, "
                    "and reports the firmware it comes back with. If a flash "
                    "is interrupted,\nhold SHIFT + ARP + OCT/MOD while "
                    "powering on, then flash again.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    f.add_argument("mff", metavar="FILE.mff", help="the firmware file to install")
    # default=None, NOT []: argparse APPENDS to a non-empty default.
    f.add_argument("--only", type=I.image_number_arg, action="append",
                   default=None, metavar="IMAGE",
                   help="write only this image: %s. Repeatable (default: "
                        "every image in the file)" % ", ".join(I.NAMES.values()))
    f.add_argument("--target", dest="only", type=I.image_number_arg,
                   action="append", help=HIDE)
    f.add_argument("--plan", action="store_true",
                   help="print the USB requests it would send, and stop. "
                        "Needs no instrument")
    f.add_argument("--yes", action="store_true",
                   help="flash without asking")
    f.add_argument("--enter", action="store_true", help=HIDE)
    a = ap.parse_args()
    return {"probe": cmd_probe, "watch": cmd_watch,
            "enter": cmd_enter, "flash": cmd_flash}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())

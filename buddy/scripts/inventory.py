"""Inventory: compare the buddy .py files on a device against the repo.

Read-only diagnostic for event-day helpers. Connects over USB-serial,
asks the device for its ``/flash/`` and ``/flash/apps/`` contents
(filenames + sizes), and prints a row per file showing how it lines
up with what's in ``buddy/device/`` on disk.

Exit code is 0 if every file matches, 1 if anything is missing,
extra, or stale (different size). Run this before re-provisioning
a device with ``m5-onboard`` — the m5-onboard skill will silently
wipe apps from ``/flash/apps/`` that aren't in the local bundle,
so it's worth seeing the current state first.

Usage:
    python inventory.py --port /dev/cu.usbmodemXXXX
    python inventory.py --port /dev/cu.usbmodemXXXX --src ../device

Status labels:
    OK                   present on device, same size as repo
    STALE                present on device, different size
    MISSING_FROM_DEVICE  in repo, not on device
    EXTRA_ON_DEVICE      on device, not in repo
"""

from __future__ import annotations

import argparse
import ast
import glob
import os
import sys
import time

try:
    import serial
except ImportError:
    sys.stderr.write("pyserial not installed. Try: pip install pyserial\n")
    raise


_BEGIN = "INVBEGIN"
_END = "INVEND"


def _drain(s: serial.Serial, wait: float = 0.2) -> bytes:
    time.sleep(wait)
    buf = b""
    while s.in_waiting:
        buf += s.read(s.in_waiting)
        time.sleep(0.03)
    return buf


def _interrupt(s: serial.Serial) -> None:
    for _ in range(4):
        s.write(b"\x03")
        time.sleep(0.05)
    s.write(b"\r\n")
    _drain(s, wait=0.3)


def _paste(s: serial.Serial, script: str, settle: float = 0.5) -> str:
    s.write(b"\x05")  # Ctrl-E (enter paste)
    time.sleep(0.1)
    _drain(s, wait=0.1)
    for line in script.splitlines():
        s.write(line.encode() + b"\r\n")
        time.sleep(0.005)
    s.write(b"\x04")  # Ctrl-D (execute)
    return _drain(s, wait=settle).decode("utf-8", errors="replace")


def _query_device(port: str) -> dict[str, int]:
    """Return ``{relpath: size}`` for every ``.py`` under /flash/ and /flash/apps/.

    Keys use the same shape as the repo side: bare basename for root
    files, ``apps/<name>`` for apps.
    """
    # Print a sentinel-bracketed Python dict literal so we can parse
    # back without depending on whatever REPL banner / paste-mode echo
    # noise comes first.
    script = (
        "import uos\n"
        "out = {}\n"
        "for f in uos.listdir('/flash'):\n"
        "    if f.endswith('.py'):\n"
        "        try:\n"
        "            out[f] = uos.stat('/flash/' + f)[6]\n"
        "        except OSError:\n"
        "            pass\n"
        "try:\n"
        "    apps = uos.listdir('/flash/apps')\n"
        "except OSError:\n"
        "    apps = []\n"
        "for f in apps:\n"
        "    if f.endswith('.py'):\n"
        "        try:\n"
        "            out['apps/' + f] = uos.stat('/flash/apps/' + f)[6]\n"
        "        except OSError:\n"
        "            pass\n"
        "print('" + _BEGIN + "')\n"
        "print(repr(out))\n"
        "print('" + _END + "')\n"
    )

    s = serial.Serial(port, 115200, timeout=1.0)
    try:
        _interrupt(s)
        _drain(s, wait=0.3)
        out = _paste(s, script, settle=0.8)
    finally:
        s.close()

    # Slice between the sentinels. The repr() output may itself contain
    # the word "print" or stray bytes, so anchoring with the sentinels
    # is safer than splitting on newlines blindly.
    if _BEGIN not in out or _END not in out:
        raise RuntimeError(
            "device didn't respond with inventory sentinels — raw output:\n" + out
        )
    chunk = out.split(_BEGIN, 1)[1].split(_END, 1)[0]
    # Find the first '{' and matching tail; ast.literal_eval handles
    # the dict literal MicroPython produced via repr().
    start = chunk.find("{")
    end = chunk.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise RuntimeError("couldn't find dict literal in device output:\n" + chunk)
    try:
        parsed = ast.literal_eval(chunk[start : end + 1])
    except (SyntaxError, ValueError) as e:
        raise RuntimeError(
            "couldn't parse device inventory: {}\nraw: {}".format(e, chunk)
        )
    if not isinstance(parsed, dict):
        raise RuntimeError("expected dict from device, got: {!r}".format(parsed))
    return {str(k): int(v) for k, v in parsed.items()}


def _scan_repo(src_dir: str) -> dict[str, int]:
    """Return ``{relpath: size}`` for the local bundle.

    Same shape as ``_query_device``: bare basename for root files,
    ``apps/<name>`` for apps/.
    """
    out: dict[str, int] = {}
    for p in sorted(glob.glob(os.path.join(src_dir, "*.py"))):
        out[os.path.basename(p)] = os.path.getsize(p)
    apps_dir = os.path.join(src_dir, "apps")
    if os.path.isdir(apps_dir):
        for p in sorted(glob.glob(os.path.join(apps_dir, "*.py"))):
            out["apps/" + os.path.basename(p)] = os.path.getsize(p)
    return out


def _classify(repo: dict[str, int], device: dict[str, int]) -> list[tuple[str, str, str]]:
    """Build ``[(status, relpath, detail), ...]`` sorted by relpath."""
    rows: list[tuple[str, str, str]] = []
    for name in sorted(set(repo) | set(device)):
        r = repo.get(name)
        d = device.get(name)
        if r is not None and d is None:
            rows.append(("MISSING_FROM_DEVICE", name, "{} B in repo".format(r)))
        elif r is None and d is not None:
            rows.append(("EXTRA_ON_DEVICE", name, "{} B on device".format(d)))
        elif r == d:
            rows.append(("OK", name, "{} B".format(r)))
        else:
            rows.append(("STALE", name, "repo={} B device={} B".format(r, d)))
    return rows


def _print_report(rows: list[tuple[str, str, str]]) -> None:
    if not rows:
        sys.stdout.write("(no .py files on device or in repo)\n")
        return
    width_status = max(len(r[0]) for r in rows)
    width_name = max(len(r[1]) for r in rows)
    for status, name, detail in rows:
        sys.stdout.write(
            "{:<{ws}}  {:<{wn}}  {}\n".format(
                status, name, detail, ws=width_status, wn=width_name
            )
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Inventory /flash/ on a Buddy device and diff against the local repo."
    )
    ap.add_argument("--port", required=True)
    ap.add_argument(
        "--src",
        default=os.path.join(os.path.dirname(__file__), "..", "device"),
        help="Local bundle directory to compare against (default: ../device).",
    )
    args = ap.parse_args()

    src_dir = os.path.abspath(args.src)
    if not os.path.isdir(src_dir):
        sys.stderr.write("--src is not a directory: {}\n".format(src_dir))
        return 2

    sys.stderr.write("querying {} ...\n".format(args.port))
    device = _query_device(args.port)
    repo = _scan_repo(src_dir)
    rows = _classify(repo, device)
    _print_report(rows)

    drift = [r for r in rows if r[0] != "OK"]
    if drift:
        sys.stderr.write("\ndrift: {} file(s) differ\n".format(len(drift)))
        return 1
    sys.stderr.write("\nall {} file(s) match\n".format(len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

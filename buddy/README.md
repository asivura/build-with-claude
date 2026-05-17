# buddy

MicroPython app bundle for the M5Stack Cardputer-Adv. Installed onto `/flash/` by the [`m5-onboard`](../onboard/) skill — see the [monorepo README](../README.md) for the end-to-end flow.

## What's on the device

```
/flash/
├── main.py              launcher menu (replaces UIFlow's boot flow)
├── buddy_*.py           shared libs (BLE, UI, state, protocol, chars)
├── burst_frames.py      sprite frames
└── apps/
    ├── claude_buddy.py  BLE client that pairs with Claude.app's Hardware Buddy
    ├── hello_cardputer.py
    └── snake.py
```

`main.py` scans `/flash/apps/` at boot and shows every `.py` as a menu entry. Drop a new file in there, re-run `m5-onboard go` (or `install_apps.py --src buddy`), and it shows up.

## Claude Buddy (BLE)

Open Claude → Developer menu → **Hardware Buddy** → Connect. BLE-only. Stats (approvals / denials / level) persist across reboots via NVS under the `buddy` namespace.

## Iterating on device code

`scripts/` has dev tooling for editing device sources without re-running the full onboard flow:

```bash
# Push a subset of files over USB-serial
python3 scripts/push.py --port /dev/cu.usbmodem1101 --files apps/snake.py

# Watch device logs
python3 scripts/tail_serial.py --port /dev/cu.usbmodem1101

# One-shot REPL exec
python3 scripts/repl_run.py --port /dev/cu.usbmodem1101 --script "import os; print(os.listdir('/flash'))"
```

`gen_burst_frames.py` regenerates `burst_frames.py` from source sprites.

## Updating apps on an already-provisioned device

Two tools, very different blast radius. Reach for the right one.

- **`scripts/push.py` — additive, safe.** Discovers every `*.py` under `buddy/device/` and `buddy/device/apps/` and uploads them. Never deletes anything that's already on the device. This is what you want 99% of the time at an event: a device boots, an app is missing, push it on.

  ```bash
  python3 scripts/push.py --port /dev/cu.usbmodem1101              # push every .py
  python3 scripts/push.py --port /dev/cu.usbmodem1101 --files snake.py
  ```

- **`m5-onboard` skill — destructive, full re-provision.** Wipes flash, reflashes UIFlow firmware, reinstalls the bundle. Use only for a fresh-from-the-box device or one that's truly bricked. **Warning: if your local repo is missing apps that are on the device, `m5-onboard` will silently delete them from `/flash/apps/`.** Run `inventory.py` first to see what you'd be wiping.

- **`scripts/inventory.py` — read-only diagnostic.** Lists what `*.py` files are on the device and diffs them against the local repo. Marks each as `OK`, `STALE` (size mismatch), `MISSING_FROM_DEVICE`, or `EXTRA_ON_DEVICE`. Exits non-zero if anything differs. Run before re-provisioning, or any time you suspect a device is out of sync.

  ```bash
  python3 scripts/inventory.py --port /dev/cu.usbmodem1101
  ```

## References

- `references/` — BLE protocol notes for the Claude Buddy app

## License

Apache 2.0 — see the [root LICENSE](../LICENSE) and [LICENSE-THIRD-PARTY.md](../LICENSE-THIRD-PARTY.md).

#!/usr/bin/env python3
"""Vial/RMK キーボードのファームウェア ビルド+書き込み。

Usage:
    # BootloaderJump のみ
    python flash_firmware.py --jump-only

    # Jump + 書き込み
    python flash_firmware.py konohana-rmk-dfu.zip

    # Build + Jump + 書き込み
    python flash_firmware.py --build --firmware-dir ./firmware

    # プロジェクト名・デバイスタイプを指定
    python flash_firmware.py --build --name my-keyboard --device-type 0x0052
"""

import argparse
import glob
import os
import struct
import subprocess
import sys
import time

# --- Vial Raw HID constants ---
VIAL_USAGE_PAGE = 0xFF60
VIAL_USAGE = 0x61
REPORT_SIZE = 32
ID_BOOTLOADER_JUMP = 0x0B

# --- Defaults ---
DEFAULT_BAUD_RATE = 115200
DEFAULT_DEVICE_TYPE = 0x0052  # nRF52840
DEFAULT_HEX_TASK = "objcopy"
DFU_WAIT_TIMEOUT = 15
DFU_POLL_INTERVAL = 0.5


# ---------------------------------------------------------------------------
# HID / BootloaderJump
# ---------------------------------------------------------------------------

def parse_usage_from_report_descriptor(desc_bytes):
    """Extract (usage_page, collection_usage) from a HID report descriptor."""
    usage_page = 0
    pending_usage = 0
    i = 0
    while i < len(desc_bytes):
        b = desc_bytes[i]
        if b == 0xC0:
            break
        bSize = b & 0x03
        bType = (b >> 2) & 0x03
        bTag = (b >> 4) & 0x0F
        if bSize == 3:
            bSize = 4
        payload = desc_bytes[i + 1 : i + 1 + bSize]
        i += 1 + bSize

        if bType == 0:  # Main
            if bTag == 0xA:  # Collection
                return usage_page, pending_usage
            if bTag in (0x8, 0x9, 0xB):
                pending_usage = 0
        elif bType == 1:  # Global
            if bTag == 0x0 and bSize == 2:
                usage_page = struct.unpack("<H", payload)[0]
            elif bTag == 0x0 and bSize == 1:
                usage_page = payload[0]
        elif bType == 2:  # Local
            if bTag == 0x0 and bSize == 1:
                pending_usage = payload[0]
            elif bTag == 0x0 and bSize == 2:
                pending_usage = struct.unpack("<H", payload)[0]
    return 0, 0


def find_hidraw_for_device(vendor_id=None, product_id=None):
    """Find hidraw nodes and return (node, vid, pid, usage_page, usage)."""
    results = []
    for uevent_path in glob.glob("/sys/class/hidraw/hidraw*/device/uevent"):
        try:
            with open(uevent_path) as f:
                content = f.read()
            hid_id_line = [l for l in content.splitlines() if l.startswith("HID_ID=")]
            if not hid_id_line:
                continue
            parts = hid_id_line[0].split(":")
            if len(parts) != 3:
                continue
            found_vid = int(parts[1], 16)
            found_pid = int(parts[2], 16)
            if vendor_id is not None and found_vid != vendor_id:
                continue
            if product_id is not None and found_pid != product_id:
                continue
        except (OSError, UnicodeDecodeError):
            continue

        hidraw_name = uevent_path.split("/")[4]
        desc_path = f"/sys/class/hidraw/{hidraw_name}/device/report_descriptor"
        try:
            with open(desc_path, "rb") as f:
                descriptor = f.read()
        except OSError:
            continue

        usage_page, usage = parse_usage_from_report_descriptor(descriptor)
        node = f"/dev/{hidraw_name}"
        results.append((node, found_vid, found_pid, usage_page, usage))
    return results


def find_vial_device(vid=None, pid=None):
    """Find the Vial Raw HID device (usage page 0xFF60, usage 0x61)."""
    # Strategy 1: sysfs descriptor parsing (Linux)
    devices = find_hidraw_for_device(vid, pid if vid is not None else None)
    for node, found_vid, found_pid, up, u in devices:
        if up == VIAL_USAGE_PAGE and u == VIAL_USAGE:
            return {"path": node, "vendor_id": found_vid, "product_id": found_pid}

    # Strategy 2: hidapi enumerate (macOS/Windows)
    try:
        import hid
        for d in hid.enumerate():
            if d.get("usage_page") == VIAL_USAGE_PAGE and d.get("usage") == VIAL_USAGE:
                if vid is not None and d["vendor_id"] != vid:
                    continue
                if pid is not None and d["product_id"] != pid:
                    continue
                return d
    except ImportError:
        pass

    return None


def jump_to_bootloader(device_info):
    """Send VIA BootloaderJump command (0x0B) as a 32-byte HID report."""
    path = device_info.get("path")
    if path:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            os.write(fd, b"\x00" + bytes([ID_BOOTLOADER_JUMP]) + b"\x00" * (REPORT_SIZE - 1))
        finally:
            os.close(fd)
    else:
        import hid as hid_mod
        dev = hid_mod.device()
        dev.open(device_info["vendor_id"], device_info["product_id"])
        report = [0] * REPORT_SIZE
        report[0] = ID_BOOTLOADER_JUMP
        dev.write(bytes(report))
        dev.close()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build_firmware(firmware_dir, name, hex_task, device_type):
    """Build hex → generate DFU zip. Returns the zip path."""
    env = os.environ.copy()
    env["RUST_MIN_STACK"] = "16777216"
    print(f"  cargo make {hex_task} ({firmware_dir})")
    if not run(["cargo", "make", hex_task], cwd=firmware_dir, env=env):
        print("エラー: ビルドに失敗", file=sys.stderr)
        sys.exit(1)

    hex_path = os.path.join(firmware_dir, f"{name}.hex")
    zip_path = os.path.join(firmware_dir, f"{name}-dfu.zip")

    print(f"  adafruit-nrfutil dfu genpkg → {zip_path}")
    if not run([
        "adafruit-nrfutil", "dfu", "genpkg",
        "--dev-type", f"0x{device_type:04X}",
        "--application", hex_path,
        zip_path,
    ]):
        print("エラー: DFU zip 生成に失敗", file=sys.stderr)
        sys.exit(1)

    return zip_path


# ---------------------------------------------------------------------------
# DFU
# ---------------------------------------------------------------------------

def run(cmd, **kwargs):
    result = subprocess.run(cmd, **kwargs)
    return result.returncode == 0


def wait_for_dfu_device(before_devices, timeout=DFU_WAIT_TIMEOUT):
    start = time.time()
    while time.time() - start < timeout:
        time.sleep(DFU_POLL_INTERVAL)
        after = set(glob.glob("/dev/ttyACM*"))
        new_devices = sorted(after - before_devices)
        if new_devices:
            return new_devices[0]
    return None


def run_dfu(device, package, baud_rate):
    return run([
        "adafruit-nrfutil", "dfu", "serial",
        "--package", package,
        "-p", device,
        "-b", str(baud_rate),
    ])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Vial/RMK キーボードのファームウェア ビルド+書き込み"
    )
    parser.add_argument("package", nargs="?", help="DFU zip ファイル。--build 時は省略可")
    parser.add_argument("--jump-only", action="store_true", help="BootloaderJump のみ実行 (書き込みしない)")
    parser.add_argument("--build", action="store_true", help="ビルドしてから書き込む")
    parser.add_argument("--name", default="konohana-rmk", help="プロジェクト名 (デフォルト: konohana-rmk)")
    parser.add_argument("--firmware-dir", default=".", help="ファームウェアソースディレクトリ (デフォルト: カレント)")
    parser.add_argument("--hex-task", default=DEFAULT_HEX_TASK, help=f"cargo make の hex タスク名 (デフォルト: {DEFAULT_HEX_TASK})")
    parser.add_argument("--device-type", type=lambda x: int(x, 0), default=DEFAULT_DEVICE_TYPE,
                        help=f"DFU デバイスタイプ (デフォルト: 0x{DEFAULT_DEVICE_TYPE:04X} = nRF52840)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD_RATE, help=f"シリアルボーレート (デフォルト: {DEFAULT_BAUD_RATE})")
    parser.add_argument("--device", help="シリアルデバイス (例: /dev/ttyACM0)。指定なしなら自動検出")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=None, help="USB Vendor ID")
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=None, help="USB Product ID")
    args = parser.parse_args()

    # --- Jump only ---
    if args.jump_only:
        print("Vial HID デバイスを検索中...", flush=True)
        device_info = find_vial_device(args.vid, args.pid)
        if device_info is None:
            print("エラー: Vial HID デバイスが見つかりません", file=sys.stderr)
            sys.exit(1)
        name = device_info.get("product_string") or device_info.get("path", "")
        print(f"デバイス検出: {name} (VID=0x{device_info['vendor_id']:04X} PID=0x{device_info['product_id']:04X})")
        jump_to_bootloader(device_info)
        print("BootloaderJump 送信完了")
        return

    # --- Build ---
    if args.build:
        print("Step 0: ビルド中...")
        args.package = build_firmware(args.firmware_dir, args.name, args.hex_task, args.device_type)
    elif args.package is None:
        parser.error("DFU zip ファイルを指定するか、--build を使用してください")

    if not os.path.isfile(args.package):
        print(f"エラー: {args.package} が見つかりません", file=sys.stderr)
        sys.exit(1)

    print(f"  パッケージ: {args.package}")

    # Capture existing devices before jump
    before_devices = set(glob.glob("/dev/ttyACM*"))

    # Step 1: BootloaderJump
    print("Step 1: ブートローダにジャンプ中...")
    print("  Vial HID デバイスを検索中...", flush=True)
    device_info = find_vial_device(args.vid, args.pid)
    if device_info is None:
        print("エラー: Vial HID デバイスが見つかりません", file=sys.stderr)
        sys.exit(1)
    jump_to_bootloader(device_info)
    print("  BootloaderJump 送信完了")

    # Step 2: Wait for DFU device
    print(f"Step 2: DFU デバイスの待機中... ({DFU_WAIT_TIMEOUT}秒 タイムアウト)")
    if args.device:
        device = args.device
        if not os.path.exists(device):
            print(f"エラー: {device} が見つかりません", file=sys.stderr)
            sys.exit(1)
    else:
        device = wait_for_dfu_device(before_devices)
        if device is None:
            print("エラー: DFU デバイスが検出されませんでした", file=sys.stderr)
            sys.exit(1)

    print(f"  DFU デバイス検出: {device}")

    # Step 3: DFU write
    print("Step 3: 書き込み中...")
    if not run_dfu(device, args.package, args.baud):
        print("エラー: DFU 書き込みに失敗", file=sys.stderr)
        sys.exit(1)

    print("書き込み完了 — デバイスは自動的に再起動します")


if __name__ == "__main__":
    main()

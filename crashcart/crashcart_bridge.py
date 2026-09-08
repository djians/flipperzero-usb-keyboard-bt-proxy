"""Safety-gated clipboard-to-Flipper Zero HID bridge.

This client supports both the repository's three-byte FAP protocol and the
newer Android KB Bridge six-byte protocol. It keeps one BLE connection open,
stages clipboard text with one hotkey, and transmits it with a second hotkey.
It deliberately never maps or sends Enter/Return.
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import hashlib
import logging
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol

import pyperclip
from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

LOG = logging.getLogger("flipper-crashcart")

# Flipper's STM32 BLE stack stores the 128-bit UUID arrays least-significant
# byte first. These are the canonical UUID strings exposed by desktop BLE APIs.
TZOIKER_SERVICE_UUID: Final = "95409655-8cd1-adaf-7945-e07d08207091"
TZOIKER_CHARACTERISTIC_UUID: Final = "c1e82a3b-d9b9-ddab-c745-1e2503c7b2b4"
ANDROID_KB_SERVICE_UUID: Final = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
ANDROID_KB_TX_UUID: Final = "19ed82ae-ed21-4c9d-4145-228e62fe0000"
DEFAULT_NAME_PREFIX: Final = "UsbKbBtP"

LEFT_SHIFT: Final = 0x02
ENTER_USAGE: Final = 0x28
KEYPAD_ENTER_USAGE: Final = 0x58


def _build_ascii_hid_map() -> dict[str, tuple[int, int]]:
    """Return a US-keyboard ASCII-to-(usage, modifier) map."""

    result: dict[str, tuple[int, int]] = {" ": (0x2C, 0)}

    for offset, letter in enumerate("abcdefghijklmnopqrstuvwxyz"):
        usage = 0x04 + offset
        result[letter] = (usage, 0)
        result[letter.upper()] = (usage, LEFT_SHIFT)

    for offset, digit in enumerate("123456789"):
        result[digit] = (0x1E + offset, 0)
    result["0"] = (0x27, 0)

    unshifted = {
        "-": 0x2D,
        "=": 0x2E,
        "[": 0x2F,
        "]": 0x30,
        "\\": 0x31,
        ";": 0x33,
        "'": 0x34,
        "`": 0x35,
        ",": 0x36,
        ".": 0x37,
        "/": 0x38,
    }
    result.update({char: (usage, 0) for char, usage in unshifted.items()})

    shifted = {
        "!": 0x1E,
        "@": 0x1F,
        "#": 0x20,
        "$": 0x21,
        "%": 0x22,
        "^": 0x23,
        "&": 0x24,
        "*": 0x25,
        "(": 0x26,
        ")": 0x27,
        "_": 0x2D,
        "+": 0x2E,
        "{": 0x2F,
        "}": 0x30,
        "|": 0x31,
        ":": 0x33,
        '"': 0x34,
        "~": 0x35,
        "<": 0x36,
        ">": 0x37,
        "?": 0x38,
    }
    result.update({char: (usage, LEFT_SHIFT) for char, usage in shifted.items()})
    return result


ASCII_HID: Final = _build_ascii_hid_map()


class BridgeProtocol(str, Enum):
    AUTO = "auto"
    TZOIKER = "tzoiker"
    ANDROID_KB = "android-kb"


class Action(Enum):
    STAGE = "stage"
    SEND = "send"
    CANCEL = "cancel"


class WindowsHotkeyListener:
    """Small Win32 global-hotkey message pump with no keyboard-hook dependency."""

    WM_HOTKEY: Final = 0x0312
    WM_QUIT: Final = 0x0012
    WM_USER: Final = 0x0400
    PM_NOREMOVE: Final = 0x0000
    MOD_CONTROL: Final = 0x0002
    MOD_SHIFT: Final = 0x0004
    MOD_NOREPEAT: Final = 0x4000
    HOTKEYS: Final = {
        1: (Action.STAGE, 0x46),  # F
        2: (Action.SEND, 0x47),  # G
        3: (Action.CANCEL, 0x58),  # X
    }

    def __init__(self, callback: Callable[[Action], None]) -> None:
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._error: RuntimeError | None = None

    def start(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("The global-hotkey client currently requires Windows")
        self._thread = threading.Thread(
            target=self._run,
            name="flipper-crashcart-hotkeys",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("Timed out while starting Windows global hotkeys")
        if self._error:
            raise self._error

    def _run(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        message = wintypes.MSG()
        self._thread_id = int(kernel32.GetCurrentThreadId())

        # Create this thread's Win32 message queue before stop() can post to it.
        user32.PeekMessageW(
            ctypes.byref(message), None, self.WM_USER, self.WM_USER, self.PM_NOREMOVE
        )
        registered: list[int] = []
        modifiers = self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT
        for hotkey_id, (_action, virtual_key) in self.HOTKEYS.items():
            if not user32.RegisterHotKey(None, hotkey_id, modifiers, virtual_key):
                self._error = RuntimeError(
                    "Could not register Ctrl+Shift+F/G/X; another app may use one "
                    "of those hotkeys"
                )
                break
            registered.append(hotkey_id)

        self._ready.set()
        try:
            if self._error:
                return
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    return
                if result == -1:
                    self._error = RuntimeError("Windows hotkey message loop failed")
                    return
                if message.message == self.WM_HOTKEY:
                    item = self.HOTKEYS.get(int(message.wParam))
                    if item:
                        self.callback(item[0])
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)

    def stop(self) -> None:
        if self._thread_id is not None:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2)


class GattWriter(Protocol):
    async def write_gatt_char(
        self,
        char_specifier: BleakGATTCharacteristic,
        data: bytes,
        response: bool | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class Timing:
    key_down_seconds: float = 0.060
    key_gap_seconds: float = 0.090
    checkpoint_every: int = 8
    checkpoint_seconds: float = 0.250

    def __post_init__(self) -> None:
        if self.key_down_seconds < 0 or self.key_gap_seconds < 0:
            raise ValueError("Key timing values must not be negative")
        if self.checkpoint_every < 1:
            raise ValueError("checkpoint_every must be at least 1")
        if self.checkpoint_seconds < 0:
            raise ValueError("checkpoint_seconds must not be negative")


def validate_text(text: str, *, max_characters: int) -> str:
    """Validate clipboard content without silently changing a command."""

    if not text:
        raise ValueError("Clipboard is empty")
    if len(text) > max_characters:
        raise ValueError(
            f"Clipboard has {len(text)} characters; limit is {max_characters}"
        )
    if "\r" in text or "\n" in text:
        raise ValueError("Clipboard contains a line break; Enter is never transmitted")
    if "```" in text:
        raise ValueError(
            "Clipboard contains a Markdown code fence; copy only the command"
        )

    unsupported = sorted({char for char in text if char not in ASCII_HID})
    if unsupported:
        rendered = ", ".join(repr(char) for char in unsupported[:8])
        suffix = " ..." if len(unsupported) > 8 else ""
        raise ValueError(
            f"Unsupported non-US-keyboard character(s): {rendered}{suffix}"
        )
    return text


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def render_preview(text: str, *, width: int = 160) -> str:
    escaped = text.replace("\t", "\\t")
    if len(escaped) <= width:
        return escaped
    return f"{escaped[: width - 1]}…"


def event_bytes(
    *,
    pressed: bool,
    usage: int,
    modifiers: int,
    protocol: BridgeProtocol = BridgeProtocol.TZOIKER,
) -> bytes:
    if not 0 <= usage <= 0xFF or not 0 <= modifiers <= 0xFF:
        raise ValueError("HID usage and modifiers must each fit in one byte")
    if usage in {ENTER_USAGE, KEYPAD_ENTER_USAGE}:
        raise ValueError("Enter/Return HID usage is forbidden")
    if protocol is BridgeProtocol.TZOIKER:
        return bytes((1 if pressed else 0, usage, modifiers))
    if protocol is BridgeProtocol.ANDROID_KB:
        event = 0x01 if pressed else 0x02
        return bytes((0xFB, 0x4B, 0x03, event, modifiers, usage))
    raise ValueError("A concrete bridge protocol is required before sending")


async def send_text_events(
    writer: GattWriter,
    characteristic: BleakGATTCharacteristic,
    text: str,
    timing: Timing,
    protocol: BridgeProtocol = BridgeProtocol.TZOIKER,
) -> None:
    """Send validated text as paced key-down/key-up events."""

    validate_text(text, max_characters=max(len(text), 1))
    for position, char in enumerate(text, start=1):
        usage, modifiers = ASCII_HID[char]
        await writer.write_gatt_char(
            characteristic,
            event_bytes(
                pressed=True,
                usage=usage,
                modifiers=modifiers,
                protocol=protocol,
            ),
            response=False,
        )
        await asyncio.sleep(timing.key_down_seconds)
        await writer.write_gatt_char(
            characteristic,
            event_bytes(
                pressed=False,
                usage=usage,
                modifiers=modifiers,
                protocol=protocol,
            ),
            response=False,
        )
        await asyncio.sleep(timing.key_gap_seconds)
        if position % timing.checkpoint_every == 0:
            await asyncio.sleep(timing.checkpoint_seconds)


class FlipperConnection:
    def __init__(
        self,
        *,
        device_name: str | None,
        name_prefix: str,
        scan_timeout: float,
        characteristic_uuid: str | None,
        protocol: BridgeProtocol,
    ) -> None:
        self.device_name = device_name
        self.name_prefix = name_prefix
        self.scan_timeout = scan_timeout
        self.characteristic_uuid = (
            characteristic_uuid.lower() if characteristic_uuid else None
        )
        self.requested_protocol = protocol
        self.active_protocol: BridgeProtocol | None = None
        self.client: BleakClient | None = None
        self.characteristic: BleakGATTCharacteristic | None = None
        self._send_lock = asyncio.Lock()

    def _matches(self, device: BLEDevice, advertisement: AdvertisementData) -> bool:
        advertised_name = advertisement.local_name or device.name
        if self.device_name:
            return advertised_name == self.device_name
        return bool(advertised_name and advertised_name.startswith(self.name_prefix))

    async def _find_device(self) -> BLEDevice:
        LOG.info(
            "Scanning for %s...",
            self.device_name or f"a device beginning with {self.name_prefix!r}",
        )
        device = await BleakScanner.find_device_by_filter(
            self._matches,
            timeout=self.scan_timeout,
        )
        if device is None:
            raise RuntimeError("Flipper BLE service was not found")
        LOG.info("Found %s (%s)", device.name or "unnamed device", device.address)
        return device

    def _disconnected(self, _client: BleakClient) -> None:
        LOG.warning("Flipper disconnected; the next Send will reconnect")
        self.characteristic = None

    def _select_characteristic(
        self,
    ) -> tuple[BleakGATTCharacteristic, BridgeProtocol]:
        if self.client is None:
            raise RuntimeError("BLE client is not initialized")

        writable: list[BleakGATTCharacteristic] = []
        candidates: list[tuple[str, BridgeProtocol]] = []
        if self.characteristic_uuid:
            inferred = {
                TZOIKER_CHARACTERISTIC_UUID: BridgeProtocol.TZOIKER,
                ANDROID_KB_TX_UUID: BridgeProtocol.ANDROID_KB,
            }.get(self.characteristic_uuid)
            if self.requested_protocol is BridgeProtocol.AUTO and inferred is None:
                raise RuntimeError(
                    "A custom --characteristic-uuid also requires an explicit --protocol"
                )
            candidates.append(
                (self.characteristic_uuid, inferred or self.requested_protocol)
            )
        elif self.requested_protocol in (
            BridgeProtocol.AUTO,
            BridgeProtocol.ANDROID_KB,
        ):
            candidates.append((ANDROID_KB_TX_UUID, BridgeProtocol.ANDROID_KB))
        if self.requested_protocol in (BridgeProtocol.AUTO, BridgeProtocol.TZOIKER):
            candidates.append((TZOIKER_CHARACTERISTIC_UUID, BridgeProtocol.TZOIKER))

        for service in self.client.services:
            for characteristic in service.characteristics:
                uuid = characteristic.uuid.lower()
                properties = {value.lower() for value in characteristic.properties}
                if "write-without-response" in properties:
                    writable.append(characteristic)
                for expected_uuid, protocol in candidates:
                    if uuid == expected_uuid:
                        if "write-without-response" not in properties:
                            raise RuntimeError(
                                f"Keyboard characteristic {uuid} is not writable without response"
                            )
                        return characteristic, protocol

        if self.characteristic_uuid:
            raise RuntimeError(
                f"Requested GATT characteristic {self.characteristic_uuid} was not found"
            )
        if self.requested_protocol is not BridgeProtocol.AUTO and len(writable) == 1:
            LOG.warning(
                "Expected characteristic UUID was absent; using the only writable "
                "characteristic: %s",
                writable[0].uuid,
            )
            return writable[0], self.requested_protocol
        if not writable:
            raise RuntimeError(
                "No write-without-response GATT characteristic was found"
            )
        available = ", ".join(item.uuid for item in writable)
        raise RuntimeError(
            "Expected Android KB Bridge or tzoiker keyboard characteristic was not "
            "found. Select --protocol and, if needed, --characteristic-uuid. "
            f"Writable candidates: {available}"
        )

    async def connect(self) -> None:
        if self.client and self.client.is_connected and self.characteristic:
            return

        await self.disconnect()
        device = await self._find_device()
        self.client = BleakClient(
            device,
            disconnected_callback=self._disconnected,
            timeout=self.scan_timeout,
            pair=True,
            winrt={"use_cached_services": False},
        )
        await self.client.connect()
        self.characteristic, self.active_protocol = self._select_characteristic()
        await asyncio.sleep(0.5)
        LOG.info(
            "Connected with %s protocol; keyboard characteristic is %s",
            self.active_protocol.value,
            self.characteristic.uuid,
        )

    async def disconnect(self) -> None:
        client, self.client = self.client, None
        self.characteristic = None
        self.active_protocol = None
        if client and client.is_connected:
            await client.disconnect()

    async def send(self, text: str, timing: Timing) -> None:
        async with self._send_lock:
            try:
                await self.connect()
                if (
                    self.client is None
                    or self.characteristic is None
                    or self.active_protocol is None
                ):
                    raise RuntimeError("Flipper connection is not ready")
                await send_text_events(
                    self.client,
                    self.characteristic,
                    text,
                    timing,
                    self.active_protocol,
                )
            except Exception as error:
                raise RuntimeError(
                    "Transmission stopped. The target may contain a partial command; "
                    "do not press Enter. Clear the target line, restart the Flipper app "
                    "if a key appears stuck, then stage the complete command again."
                ) from error


@dataclass
class StagedText:
    value: str
    fingerprint: str


class CrashCartApp:
    def __init__(
        self,
        connection: FlipperConnection,
        *,
        timing: Timing,
        max_characters: int,
    ) -> None:
        self.connection = connection
        self.timing = timing
        self.max_characters = max_characters
        self.actions: asyncio.Queue[Action] = asyncio.Queue()
        self.staged: StagedText | None = None
        self.listener: WindowsHotkeyListener | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def _enqueue(self, action: Action) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.actions.put_nowait, action)

    def _start_hotkeys(self) -> None:
        self.listener = WindowsHotkeyListener(self._enqueue)
        self.listener.start()

    def _stage_clipboard(self) -> None:
        raw = pyperclip.paste()
        text = validate_text(raw, max_characters=self.max_characters)
        self.staged = StagedText(text, text_fingerprint(text))
        print("\nSTAGED — review before sending")
        print(f"Length: {len(text)} | SHA-256 prefix: {self.staged.fingerprint}")
        print(f"Preview: {render_preview(text)}")
        print("Press Ctrl+Shift+G to type it, or Ctrl+Shift+X to cancel.")

    async def _send_staged(self) -> None:
        if self.staged is None:
            print("Nothing is staged. Press Ctrl+Shift+F first.")
            return
        staged = self.staged
        self.staged = None
        print(
            f"Sending {len(staged.value)} characters "
            f"(SHA-256 prefix {staged.fingerprint})..."
        )
        await self.connection.send(staged.value, self.timing)
        print(
            "Typed successfully. Inspect the target screen, then press Enter yourself."
        )

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._start_hotkeys()
        print("Flipper Zero AI Crash Cart")
        print("Ctrl+Shift+F  stage clipboard")
        print("Ctrl+Shift+G  send staged text")
        print("Ctrl+Shift+X  cancel staged text")
        print("Ctrl+C        quit")
        print("Enter/Return and multiline clipboard text are always blocked.\n")

        try:
            await self.connection.connect()
            while True:
                action = await self.actions.get()
                try:
                    if action is Action.STAGE:
                        self._stage_clipboard()
                    elif action is Action.SEND:
                        await self._send_staged()
                    elif action is Action.CANCEL:
                        self.staged = None
                        print("Staged text canceled.")
                except (RuntimeError, ValueError) as error:
                    LOG.error("%s", error)
                except Exception:
                    LOG.exception("Unexpected bridge error")
        finally:
            if self.listener is not None:
                self.listener.stop()
            await self.connection.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stage clipboard text and type it through a Flipper Zero BLE/USB bridge."
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--device-name", help="Exact BLE name shown by the Flipper app")
    target.add_argument(
        "--name-prefix",
        default=DEFAULT_NAME_PREFIX,
        help=f"BLE name prefix when --device-name is omitted (default: {DEFAULT_NAME_PREFIX})",
    )
    parser.add_argument(
        "--characteristic-uuid",
        help="Override the Flipper keyboard GATT characteristic UUID",
    )
    parser.add_argument(
        "--protocol",
        choices=[item.value for item in BridgeProtocol],
        default=BridgeProtocol.AUTO.value,
        help="BLE frame protocol (default: detect known characteristic UUID)",
    )
    parser.add_argument("--scan-timeout", type=float, default=30.0)
    parser.add_argument("--max-characters", type=int, default=2000)
    parser.add_argument("--key-down-ms", type=float, default=60.0)
    parser.add_argument("--key-gap-ms", type=float, default=90.0)
    parser.add_argument("--checkpoint-every", type=int, default=8)
    parser.add_argument("--checkpoint-ms", type=float, default=250.0)
    parser.add_argument("--verbose", action="store_true")
    return parser


def parse_timing(args: argparse.Namespace) -> Timing:
    return Timing(
        key_down_seconds=args.key_down_ms / 1000,
        key_gap_seconds=args.key_gap_ms / 1000,
        checkpoint_every=args.checkpoint_every,
        checkpoint_seconds=args.checkpoint_ms / 1000,
    )


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    if args.max_characters < 1:
        raise SystemExit("--max-characters must be at least 1")

    connection = FlipperConnection(
        device_name=args.device_name,
        name_prefix=args.name_prefix,
        scan_timeout=args.scan_timeout,
        characteristic_uuid=args.characteristic_uuid,
        protocol=BridgeProtocol(args.protocol),
    )
    app = CrashCartApp(
        connection,
        timing=parse_timing(args),
        max_characters=args.max_characters,
    )
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        print("\nStopped.")
    # Bleak exposes platform-specific transport exceptions; keep the CLI error
    # concise while detailed tracebacks remain available through --verbose.
    except Exception as error:  # noqa: BLE001
        LOG.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

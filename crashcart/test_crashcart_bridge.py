import unittest
from types import SimpleNamespace

from crashcart_bridge import (
    ASCII_HID,
    BridgeProtocol,
    FlipperConnection,
    Timing,
    event_bytes,
    render_preview,
    send_text_events,
    text_fingerprint,
    validate_text,
)


class FakeWriter:
    def __init__(self) -> None:
        self.writes: list[tuple[object, bytes, bool | None]] = []

    async def write_gatt_char(
        self, characteristic: object, data: bytes, response: bool | None = None
    ) -> None:
        self.writes.append((characteristic, data, response))


class ValidateTextTests(unittest.TestCase):
    def test_all_printable_us_ascii_is_mapped(self) -> None:
        printable = "".join(chr(code) for code in range(32, 127))
        self.assertEqual(validate_text(printable, max_characters=95), printable)
        self.assertEqual(set(printable), set(ASCII_HID))

    def test_enter_and_multiline_are_blocked(self) -> None:
        for text in ("whoami\n", "a\rb", "a\r\nb"):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(ValueError, "Enter is never transmitted"),
            ):
                validate_text(text, max_characters=100)

    def test_unicode_is_rejected_without_transformation(self) -> None:
        for text in ("echo ‘hello’", "echo é", "echo 🙂", "a\u2028b"):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(ValueError, "Unsupported"),
            ):
                validate_text(text, max_characters=100)

    def test_markdown_fence_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "code fence"):
            validate_text("```whoami```", max_characters=100)

    def test_length_limit_is_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit is 3"):
            validate_text("abcd", max_characters=3)

    def test_fingerprint_is_repeatable(self) -> None:
        self.assertEqual(text_fingerprint("abc"), "ba7816bf8f01")

    def test_preview_is_never_truncated(self) -> None:
        text = "x" * 500
        self.assertEqual(render_preview(text), text)


class EventTests(unittest.IsolatedAsyncioTestCase):
    def test_shifted_symbol_mapping(self) -> None:
        self.assertEqual(ASCII_HID["A"], (0x04, 0x02))
        self.assertEqual(ASCII_HID["|"], (0x31, 0x02))
        self.assertEqual(ASCII_HID["0"], (0x27, 0))

    def test_event_bytes(self) -> None:
        self.assertEqual(
            event_bytes(pressed=True, usage=0x04, modifiers=0x02),
            b"\x01\x04\x02",
        )
        self.assertEqual(
            event_bytes(pressed=False, usage=0x04, modifiers=0x02),
            b"\x00\x04\x02",
        )
        self.assertEqual(
            event_bytes(
                pressed=True,
                usage=0x04,
                modifiers=0x02,
                protocol=BridgeProtocol.ANDROID_KB,
            ),
            b"\xfb\x4b\x03\x01\x02\x04",
        )
        self.assertEqual(
            event_bytes(
                pressed=False,
                usage=0x04,
                modifiers=0x02,
                protocol=BridgeProtocol.ANDROID_KB,
            ),
            b"\xfb\x4b\x03\x02\x02\x04",
        )

    def test_enter_usage_is_forbidden_at_frame_layer(self) -> None:
        for usage in (0x28, 0x58):
            with (
                self.subTest(usage=usage),
                self.assertRaisesRegex(ValueError, "forbidden"),
            ):
                event_bytes(pressed=True, usage=usage, modifiers=0)

    async def test_each_character_has_press_then_release(self) -> None:
        writer = FakeWriter()
        characteristic = object()
        await send_text_events(
            writer,
            characteristic,  # type: ignore[arg-type]
            "A0",
            Timing(0, 0, checkpoint_every=8, checkpoint_seconds=0),
        )
        self.assertEqual(
            [data for _characteristic, data, _response in writer.writes],
            [b"\x01\x04\x02", b"\x00\x04\x02", b"\x01\x27\x00", b"\x00\x27\x00"],
        )
        self.assertTrue(all(response is False for _, _, response in writer.writes))

    async def test_android_kb_protocol_frames_are_sequential(self) -> None:
        writer = FakeWriter()
        characteristic = object()
        await send_text_events(
            writer,
            characteristic,  # type: ignore[arg-type]
            "A",
            Timing(0, 0, checkpoint_every=8, checkpoint_seconds=0),
            BridgeProtocol.ANDROID_KB,
        )
        self.assertEqual(
            [data for _characteristic, data, _response in writer.writes],
            [b"\xfb\x4b\x03\x01\x02\x04", b"\xfb\x4b\x03\x02\x02\x04"],
        )


class ConnectionSafetyTests(unittest.IsolatedAsyncioTestCase):
    def make_connection(self) -> FlipperConnection:
        return FlipperConnection(
            device_name="Flipper Test",
            name_prefix="Flipper",
            scan_timeout=1,
            characteristic_uuid=None,
            protocol=BridgeProtocol.ANDROID_KB,
        )

    def test_unknown_writable_characteristic_fails_closed(self) -> None:
        connection = self.make_connection()
        characteristic = SimpleNamespace(
            uuid="00000000-0000-0000-0000-000000000001",
            properties=["write-without-response"],
        )
        connection.client = SimpleNamespace(
            services=[SimpleNamespace(characteristics=[characteristic])]
        )  # type: ignore[assignment]

        with self.assertRaisesRegex(RuntimeError, "Expected Android KB Bridge"):
            connection._select_characteristic()

    async def test_send_failure_forces_disconnect(self) -> None:
        connection = self.make_connection()

        class FailingClient:
            is_connected = True
            disconnected = False

            async def write_gatt_char(self, *_args, **_kwargs) -> None:
                raise OSError("simulated BLE loss")

            async def disconnect(self) -> None:
                self.disconnected = True
                self.is_connected = False

        client = FailingClient()

        async def fake_connect() -> None:
            connection.client = client  # type: ignore[assignment]
            connection.characteristic = object()  # type: ignore[assignment]
            connection.active_protocol = BridgeProtocol.ANDROID_KB

        connection.connect = fake_connect  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "target may contain a partial"):
            await connection.send(
                "a",
                Timing(0, 0, checkpoint_every=8, checkpoint_seconds=0),
            )
        self.assertTrue(client.disconnected)


if __name__ == "__main__":
    unittest.main()

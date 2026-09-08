# Flipper Zero AI Crash Cart

This optional Windows-first client implements the safety-gated workflow from
the ChatGPT/Flipper crash-cart example:

`clipboard -> good PC BLE -> Flipper Zero -> USB HID -> target PC`

It supports the newer **Android KB Bridge** Flipper app used by the example and
this repository's original **USB Keyboard BT Proxy** app. The bridge keeps one
Bluetooth Low Energy connection open and sends deliberately paced HID
key-down/key-up events. It does **not** send Enter.

## Safety model

- `Ctrl+Shift+F` stages and previews the clipboard.
- `Ctrl+Shift+G` sends the staged text.
- `Ctrl+Shift+X` cancels unsent staged text. It cannot stop a transmission that
  is already in progress.
- Newlines, carriage returns, empty text, unsupported Unicode, and oversized
  payloads are rejected.
- The default command limit is 160 characters, and the stage preview is never
  truncated.
- The target computer never receives Enter/Return from this program. Read the
  target screen and press Enter on a physical keyboard only after verification.
- Use only with devices you own or are authorized to service.

This is a convenience tool, not an out-of-band management system. It cannot see
the target screen, confirm that a command arrived correctly, or undo a command.

## Prerequisites

- A Flipper Zero with microSD card and current official firmware.
- A data-capable USB-C cable from the Flipper to the target computer.
- A Bluetooth-capable Windows 10/11 computer (the "good PC").
- Python 3.11 or 3.12 on the good PC.
- A US English keyboard layout on the target, with Caps Lock off.
- Recommended: Android KB Bridge FAP `v0.5.8`, whose published binary targets
  official firmware 1.4.3 / API 87.1. If your Flipper firmware differs, use a
  matching release or build the FAP against that firmware.

## 1. Install the Flipper app

### Recommended: Android KB Bridge

Download the C-language FAP from the maintained project release (not the
educational Rust build):

- [Android KB Bridge v0.5.8 release](https://github.com/andybeg/AndroidFlipperZeroKBD/releases/tag/v0.5.8)
- File: `android_keyboard_bridge-0.5.8.fap`

Copy it to `apps/Bluetooth` on the Flipper microSD card. The binary is published
for official firmware 1.4.3 / API 87.1. If that does not match your installed
firmware, follow the project's build instructions instead of forcing the old
binary to load.

### Alternative: this repository's original FAP

The original FAP can be built against the Flipper firmware currently installed:

```powershell
git clone https://github.com/tzoiker/flipperzero-usb-keyboard-bt-proxy.git
cd flipperzero-usb-keyboard-bt-proxy\fap
py -3 -m pip install --upgrade ufbt
ufbt update
ufbt launch
```

`ufbt launch` builds the app, copies it to the connected Flipper, and launches
it. The upstream `v0.0.1a2` binary targets API 79.2 / firmware 1.2.0, so do not
use that binary on a newer firmware/API.

## 2. Install the good-PC bridge

Open PowerShell in this `crashcart` folder:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install-windows.ps1
```

The execution-policy change applies only to the current PowerShell process.

If you have not cloned this fork yet:

```powershell
git clone https://github.com/djians/flipperzero-usb-keyboard-bt-proxy.git
cd flipperzero-usb-keyboard-bt-proxy\crashcart
```

## 3. Connect

1. Pair the Flipper with Windows in **Settings > Bluetooth & devices**.
2. Connect the Flipper's USB port to the target PC.
3. On the Flipper, open **Apps > Bluetooth > Android KB Bridge**.
4. Note the Flipper's normal Bluetooth name from Windows, such as
   `Flipper MyName`.
5. On the good PC, run:

   ```powershell
   .\run-windows.ps1 --device-name "Flipper MyName" --protocol android-kb
   ```

6. Accept matching Bluetooth pairing confirmation on both devices when
   requested.

For this repository's original app, select **USB Keyboard BT Proxy > Start** and
run:

```powershell
.\run-windows.ps1 --device-name "UsbKbBtP MyFlipper" --protocol tzoiker
```

If only one nearby Flipper is running the original app, `--device-name` and
`--protocol` can normally be omitted; the bridge scans for the `UsbKbBtP`
prefix and recognizes either known GATT characteristic.

## 4. Validate before real recovery work

1. Put the target PC in a harmless text field such as Notepad or a disposable
   WinRE Command Prompt line.
2. Copy this exact test string on the good PC:

   ```text
   AbcXYZ 019 -_=+[]{}\|;:'",.<>/?`~!@#$%^&*()
   ```

3. Press `Ctrl+Shift+F`, compare the preview and length, then press
   `Ctrl+Shift+G`.
4. Compare the target text character-for-character. Do not proceed unless it is
   exact on three consecutive trials.
5. In the harmless text field, start sending a long repeated-letter test and
   close the Flipper app mid-send. Confirm transmission stops, the target does
   not keep repeating a held key, and the client warns that the line is partial.

If characters drop, increase the pacing:

```powershell
.\run-windows.ps1 --key-down-ms 80 --key-gap-ms 120 --checkpoint-ms 300
```

Keyboard mapping is US ANSI. Change the target OS keyboard layout to US English
and turn Caps Lock off so letters and punctuation arrive correctly.

## Recovery workflow

1. Photograph the target screen with your phone.
2. Review it in ChatGPT from the good PC and ask for one diagnostic command at a
   time, including what it does and how to reverse it.
3. Copy only the command text—no code-fence marks, prompt characters, or newline.
4. Stage with `Ctrl+Shift+F` and inspect the local preview.
5. Send with `Ctrl+Shift+G`.
6. Compare the target screen to the staged preview.
7. Press Enter physically only when the command is exact and understood.
8. Photograph the result and repeat.

## Troubleshooting

**Flipper not found**

- Confirm Bluetooth is enabled on the good PC and on the Flipper.
- Confirm the Flipper app is open and its screen says the service started.
- Use the exact displayed name with `--device-name`.
- Remove an obsolete OS pairing and pair again if the Flipper firmware or app
  changed its BLE identity.

**Connected, but nothing appears on the target**

- Use a data-capable USB cable; some USB-C cables provide power only.
- Reconnect the USB cable after the Flipper app starts.
- Test in a plain text field before WinRE or firmware screens.
- Confirm the target accepts ordinary USB keyboards in that environment.

**Wrong or missing punctuation**

- Select a US English keyboard layout on the target.
- Slow the three timing parameters shown above.
- Keep the good PC and Flipper close together.

**Pairing or GATT errors**

- Stop the bridge, close the Flipper app, remove the pairing from Windows, and
  start fresh.
- Use `--verbose` for diagnostic logs.
- If discovery finds multiple writable characteristics, pass the candidate UUID
  shown in the error with `--characteristic-uuid`.

## Development test

The HID mapping and no-Enter gate can be tested without a Flipper:

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_crashcart_bridge.py
```

The BLE and USB portions still require an end-to-end hardware test.

## Upstream and license

The current recommended FAP/protocol is from
[andybeg/AndroidFlipperZeroKBD](https://github.com/andybeg/AndroidFlipperZeroKBD).
Its documented protocol releases all keys on BLE disconnect and uses the
Flipper Serial service. The original firmware and GUI in this fork are from
[tzoiker/flipperzero-usb-keyboard-bt-proxy](https://github.com/tzoiker/flipperzero-usb-keyboard-bt-proxy)
under the MIT License. Its older FAP path is retained for compatibility but is
not the recommended setup and still requires a firmware-matched build and its
own hardware validation. This crash-cart client is an additive workflow for
that project and retains the upstream license and attribution.

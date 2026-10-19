// Rook Pico HID bridge — symmetric firmware for TWO soldered Picos.
//
// Hardware: two RP2040 Picos, GP0<->GP1 cross-soldered (UART0 TX<->RX) with
// common ground. One Pico plugs into the host PC (CachyOS), the other into the
// target laptop. Same binary on both.
//
// Role is implicit in the data, never stored:
//   * USB-CDC bytes in  -> copied verbatim out the UART  (this board is bridge)
//   * UART bytes in      -> parsed into USB HID reports   (this board is gadget)
// The host-side board only ever sees CDC traffic; the laptop-side board only
// ever sees UART traffic. Both behaviours run on both boards; each board simply
// exercises the one that applies to where it's plugged in.
//
// Wire frame (identical on the CDC link and the UART link, so the bridge is a
// dumb byte-for-byte pipe):
//     0xAB | type | len | payload[len] | crc8(type,len,payload)
//     type 1 = keyboard (8 bytes: mods, reserved, key[6])
//     type 2 = mouse    (7 bytes: buttons, x:i16, y:i16, wheel:i8, pan:i8)
//     type 4 = consumer (2 bytes: usage:u16 LE)
// Report IDs (1/2/4) and the mouse layout match the ESP32 rook firmware, so the
// host frame format is shared between both transports.

#include <Arduino.h>
#include <Adafruit_TinyUSB.h>
#include <string.h>
#include "disk_image.h"   // read-only FAT image of the host client (see tools/mkdisk.sh)

// ---- UART link to the partner board ----
#define LINK_BAUD     921600
#define LINK_TX_PIN   0   // GP0 = UART0 TX
#define LINK_RX_PIN   1   // GP1 = UART0 RX

// ---- frame protocol ----
#define FRAME_SYNC    0xAB
#define T_KEYBOARD    1
#define T_MOUSE       2
#define T_CONSUMER    4
#define MAX_PAYLOAD   32

// ---- HID report descriptor: keyboard(1) + custom 8-button mouse(2) + consumer(4)
// Keyboard/consumer use the stock TinyUSB macros; the mouse is hand-written to
// match the Elecom HUGE (8 buttons, 16-bit relative X/Y, wheel + AC Pan).
static const uint8_t HID_DESC[] = {
    TUD_HID_REPORT_DESC_KEYBOARD(HID_REPORT_ID(T_KEYBOARD)),

    // ---- custom mouse, Report ID 2 ----
    0x05, 0x01,        // Usage Page (Generic Desktop)
    0x09, 0x02,        // Usage (Mouse)
    0xA1, 0x01,        // Collection (Application)
    0x85, T_MOUSE,     //   Report ID (2)
    0x09, 0x01,        //   Usage (Pointer)
    0xA1, 0x00,        //   Collection (Physical)
    0x05, 0x09,        //     Usage Page (Button)
    0x19, 0x01,        //     Usage Minimum (Button 1)
    0x29, 0x08,        //     Usage Maximum (Button 8)
    0x15, 0x00,        //     Logical Minimum (0)
    0x25, 0x01,        //     Logical Maximum (1)
    0x95, 0x08,        //     Report Count (8)
    0x75, 0x01,        //     Report Size (1)
    0x81, 0x02,        //     Input (Data,Var,Abs)
    0x05, 0x01,        //     Usage Page (Generic Desktop)
    0x09, 0x30,        //     Usage (X)
    0x09, 0x31,        //     Usage (Y)
    0x16, 0x01, 0x80,  //     Logical Minimum (-32767)
    0x26, 0xFF, 0x7F,  //     Logical Maximum (32767)
    0x75, 0x10,        //     Report Size (16)
    0x95, 0x02,        //     Report Count (2)
    0x81, 0x06,        //     Input (Data,Var,Rel)
    0x09, 0x38,        //     Usage (Wheel)
    0x15, 0x81,        //     Logical Minimum (-127)
    0x25, 0x7F,        //     Logical Maximum (127)
    0x75, 0x08,        //     Report Size (8)
    0x95, 0x01,        //     Report Count (1)
    0x81, 0x06,        //     Input (Data,Var,Rel)
    0x05, 0x0C,        //     Usage Page (Consumer)
    0x0A, 0x38, 0x02,  //     Usage (AC Pan)
    0x15, 0x81,        //     Logical Minimum (-127)
    0x25, 0x7F,        //     Logical Maximum (127)
    0x75, 0x08,        //     Report Size (8)
    0x95, 0x01,        //     Report Count (1)
    0x81, 0x06,        //     Input (Data,Var,Rel)
    0xC0,              //   End Collection
    0xC0,              // End Collection

    TUD_HID_REPORT_DESC_CONSUMER(HID_REPORT_ID(T_CONSUMER)),
};

static Adafruit_USBD_HID usb_hid;

// ---- USB Mass Storage: serve the embedded read-only FAT image so the host
// client script ships *on the dongle itself*. Appears as a tiny write-protected
// "ROOK" stick on whatever this board is plugged into (both bridge and gadget,
// since the firmware is identical).
#define DISK_BLOCK_SIZE 512
static Adafruit_USBD_MSC usb_msc;

static int32_t msc_read(uint32_t lba, void* buffer, uint32_t bufsize) {
    uint32_t off = lba * DISK_BLOCK_SIZE;
    if (off >= rook_disk_len) return 0;
    if (off + bufsize > rook_disk_len) bufsize = rook_disk_len - off;
    memcpy(buffer, rook_disk + off, bufsize);
    return (int32_t)bufsize;
}
static int32_t msc_write(uint32_t, uint8_t*, uint32_t) { return -1; }  // read-only
static bool msc_writable(void) { return false; }                      // write-protected

// CRC-8 (poly 0x07) over type, len, then payload — matches the host script.
static uint8_t crc8(uint8_t type, uint8_t len, const uint8_t* p) {
    uint8_t crc = 0;
    auto step = [&](uint8_t b) {
        crc ^= b;
        for (int i = 0; i < 8; i++)
            crc = (crc & 0x80) ? (crc << 1) ^ 0x07 : (crc << 1);
    };
    step(type);
    step(len);
    for (uint8_t i = 0; i < len; i++) step(p[i]);
    return crc;
}

// Emit a decoded frame as a USB HID report. Drops it if the host hasn't opened
// the HID interface yet (i.e. this is the bridge board, nothing plugged to HID).
static void emitHID(uint8_t type, const uint8_t* body, uint8_t len) {
    if (!TinyUSBDevice.mounted()) return;
    // brief spin so a back-to-back report isn't dropped while the last is in
    // flight; bounded so we never stall the link.
    for (int i = 0; i < 200 && !usb_hid.ready(); i++) delayMicroseconds(50);
    if (!usb_hid.ready()) return;
    usb_hid.sendReport(type, body, len);
    digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));  // activity blink
}

// Incremental UART frame parser. Resyncs on SYNC, validates CRC, drops garbage.
static void feedLink(uint8_t b) {
    static uint8_t st = 0, ftype = 0, flen = 0, idx = 0, buf[MAX_PAYLOAD];
    switch (st) {
        case 0: if (b == FRAME_SYNC) st = 1; break;
        case 1: ftype = b; st = 2; break;
        case 2:
            flen = b;
            if (flen > MAX_PAYLOAD) { st = 0; break; }
            idx = 0;
            st = (flen == 0) ? 4 : 3;
            break;
        case 3:
            buf[idx++] = b;
            if (idx >= flen) st = 4;
            break;
        case 4:
            if (b == crc8(ftype, flen, buf)) emitHID(ftype, buf, flen);
            st = 0;
            break;
    }
}

void setup() {
    pinMode(LED_BUILTIN, OUTPUT);

    TinyUSBDevice.setManufacturerDescriptor("Rook");
    TinyUSBDevice.setProductDescriptor("Rook HID Bridge");

    usb_hid.setPollInterval(1);  // 1ms — fastest standard HID poll
    usb_hid.setReportDescriptor(HID_DESC, sizeof(HID_DESC));
    usb_hid.begin();

    usb_msc.setID("Rook", "KVM Client", "1.0");
    usb_msc.setCapacity(rook_disk_len / DISK_BLOCK_SIZE, DISK_BLOCK_SIZE);
    usb_msc.setReadWriteCallback(msc_read, msc_write, NULL);
    usb_msc.setWritableCallback(msc_writable);
    usb_msc.setUnitReady(true);
    usb_msc.begin();

    Serial.begin(115200);   // USB-CDC to host (baud is ignored over USB)

    Serial1.setTX(LINK_TX_PIN);
    Serial1.setRX(LINK_RX_PIN);
    Serial1.begin(LINK_BAUD);
}

void loop() {
    // bridge path: copy host CDC bytes straight to the partner over UART.
    while (Serial.available()) Serial1.write((uint8_t)Serial.read());
    // gadget path: decode partner UART bytes into HID reports.
    while (Serial1.available()) feedLink((uint8_t)Serial1.read());
}

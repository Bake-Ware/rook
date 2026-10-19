#pragma once
#include <Arduino.h>
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"
#include "USBHID.h"

extern USBHIDKeyboard Keyboard;
extern USBHIDConsumerControl Consumer;

// Report IDs on the shared TinyUSB HID interface — must match the descriptors.
//   1 = keyboard (USBHIDKeyboard, 8-byte boot report)
//   2 = mouse    (RookMouse, custom 8-button report)
//   4 = consumer (USBHIDConsumerControl, 16-bit usage)
// These line up with the ESP32 core's HID_REPORT_ID_* enum (KEYBOARD=1,
// MOUSE=2, CONSUMER_CONTROL=4), so the stock keyboard/consumer classes and our
// custom mouse coexist on one interface without an ID clash.
#define ROOK_HID_RPT_KEYBOARD 1
#define ROOK_HID_RPT_MOUSE    2
#define ROOK_HID_RPT_CONSUMER 4

// 7-byte relative mouse report: 8 buttons, 16-bit X/Y, 8-bit wheel + AC Pan.
typedef struct __attribute__((packed)) {
    uint8_t buttons;
    int16_t x;
    int16_t y;
    int8_t  wheel;
    int8_t  pan;
} RookMouseReport;

// Custom 8-button relative mouse. The stock USBHIDMouse only carries 5 buttons
// and no independent horizontal wheel, which silently drops the Elecom HUGE's
// two function buttons + hi-res/tilt scroll — the same limitation that makes
// cheap hardware KVMs mangle the trackball. We own the descriptor instead so
// every button and axis the host PC reads passes straight through.
class RookMouse : public USBHIDDevice {
public:
    RookMouse();
    void begin();
    // Raw passthrough for the realtime stream path (len must be sizeof report).
    bool sendRaw(const uint8_t* data, size_t len);
    // Send an arbitrary report id over the shared HID interface.
    bool sendReportId(uint8_t report_id, const void* data, size_t len);
    // Structured convenience (used by the kvm.mouse RPC cap).
    bool move(uint8_t buttons, int16_t dx, int16_t dy, int8_t wheel, int8_t pan);
    uint16_t _onGetDescriptor(uint8_t* buffer) override;
private:
    USBHID hid;
};

extern RookMouse Mouse;

uint8_t  lookupSpecialKey(const char* name);
uint8_t  lookupModifierKey(const char* name);
uint16_t lookupConsumerKey(const char* name);

// Realtime raw-report entry point — the LAN-direct stream path and any future
// device.usb.hid.stream capability call this. Payload is the report body only
// (no report-id prefix; SendReport prepends it):
//   report_id 1 -> 8-byte boot keyboard report
//   report_id 2 -> 7-byte RookMouseReport
//   report_id 4 -> 2-byte consumer usage
// Returns false if HID is disabled or the length is wrong for the id.
bool hidStreamReport(uint8_t report_id, const uint8_t* data, size_t len);

// Push an "everything released" state for keyboard + mouse. Sent on every KVM
// toggle so no key/button sticks down when input switches sides.
void hidReleaseAll();

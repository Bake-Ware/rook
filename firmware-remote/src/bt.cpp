#include "bt.h"
#include <NimBLEDevice.h>
#include <Preferences.h>

namespace {

const uint16_t HID_SERVICE = 0x1812, HID_REPORT = 0x2A4D;

Preferences prefs;
bool inited = false;
volatile bool pairing = false, want_connect = false, connected = false;
uint32_t pair_until = 0;
NimBLEClient* client = nullptr;
std::string saved_addr, saved_name, cand_addr, cand_name, last_hex, held_name;
uint8_t saved_type = 0, cand_type = 0;
QueueHandle_t keys;
SemaphoreHandle_t mu;
std::function<void(const char*)> logger;
struct Last { uint16_t handle; uint32_t usage; } lasts[8];
int nlasts = 0;

void logf(const char* fmt, ...) {
    char b[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    if (logger) logger(b);
}

// Consumer-page usages (what a TV remote mostly sends), by the handheld's names.
const char* consumer_name(uint32_t u) {
    switch (u) {
        case 0x41: return "ok";
        case 0x42: return "up";
        case 0x43: return "down";
        case 0x44: return "left";
        case 0x45: return "right";
        case 0x46: case 0x224: return "back";
        case 0x223: return "home";
        case 0xE9: return "vol_up";
        case 0xEA: return "vol_down";
        case 0xE2: return "mute";
        case 0x30: return "power";
        case 0x40: return "bt_menu";
        case 0xCD: return "play";
        case 0xB0: return "play";
        case 0xB1: return "pause";
        case 0xB3: return "forward";
        case 0xB4: return "rewind";
        case 0xB5: return "next";
        case 0xB6: return "prev";
        case 0x9C: return "ch_up";
        case 0x9D: return "ch_down";
        case 0x221: return "search";
        case 0x8D: return "guide";
    }
    return nullptr;
}

// Keyboard-page codes some remotes use for the same keys.
const char* keyboard_name(uint32_t k) {
    switch (k) {
        case 0x28: case 0x58: return "ok";
        case 0x29: case 0xF1: return "back";
        case 0x4F: return "right";
        case 0x50: return "left";
        case 0x51: return "down";
        case 0x52: return "up";
        case 0x4A: return "home";
        case 0x65: return "bt_menu";
        case 0x66: return "power";
        case 0x7F: return "mute";
        case 0x80: return "vol_up";
        case 0x81: return "vol_down";
    }
    return nullptr;
}

// One input report. A key is whatever non-zero value a report carries: a
// press is that value appearing, all zeros is the release.
void on_report(NimBLERemoteCharacteristic* c, uint8_t* d, size_t len, bool) {
    char hex[48];
    int at = snprintf(hex, sizeof hex, "h%u:", (unsigned)c->getHandle());
    for (size_t i = 0; i < len && i < 12 && at < (int)sizeof hex - 3; i++) at += snprintf(hex + at, sizeof hex - at, "%02x", d[i]);
    xSemaphoreTake(mu, portMAX_DELAY);
    last_hex = hex;
    xSemaphoreGive(mu);
    logf("bt: report %s", hex);
    bool keyboard = len >= 6;
    uint32_t usage = 0;
    if (keyboard) {
        for (size_t i = 2; i < len && i < 8; i++)
            if (d[i]) { usage = d[i]; break; }
    } else {
        for (size_t i = 0; i < len && i < 4; i++) usage |= (uint32_t)d[i] << (8 * i);
    }
    Last* l = nullptr;
    for (int i = 0; i < nlasts; i++)
        if (lasts[i].handle == c->getHandle()) l = &lasts[i];
    if (!l && nlasts < 8) {
        l = &lasts[nlasts++];
        l->handle = c->getHandle();
        l->usage = 0;
    }
    if (l) {
        if (l->usage == usage) return;
        l->usage = usage;
    }
    if (!usage) {
        xSemaphoreTake(mu, portMAX_DELAY);
        held_name.clear();
        xSemaphoreGive(mu);
        return;
    }
    char name[20];
    const char* known = keyboard ? keyboard_name(usage) : consumer_name(usage);
    if (known) strlcpy(name, known, sizeof name);
    else snprintf(name, sizeof name, "bt_%c%lx", keyboard ? 'k' : 'c', (unsigned long)usage);
    xSemaphoreTake(mu, portMAX_DELAY);
    held_name = name;
    xSemaphoreGive(mu);
    xQueueSend(keys, name, 0);
}

class Seen : public NimBLEAdvertisedDeviceCallbacks {
    void onResult(NimBLEAdvertisedDevice* d) override {
        if (connected || want_connect) return;
        std::string a = d->getAddress().toString();
        bool take = false;
        if (pairing) {
            bool hid = d->isAdvertisingService(NimBLEUUID(HID_SERVICE));
            if (!hid && d->haveAppearance()) {
                uint16_t ap = d->getAppearance();
                hid = (ap >= 0x03C0 && ap <= 0x03C4) || ap == 0x0180;      // HID devices, remote control
            }
            take = hid && d->getRSSI() > -85;
        } else {
            take = !saved_addr.empty() && a == saved_addr;
        }
        if (!take) return;
        cand_addr = a;
        cand_type = d->getAddress().getType();
        cand_name = d->haveName() ? d->getName() : (pairing ? std::string("remote") : saved_name);
        want_connect = true;
        NimBLEDevice::getScan()->stop();
    }
};

void scan_done(NimBLEScanResults) {}

void radio_on() {
    if (inited) return;
    NimBLEDevice::init("Rook remote");
    NimBLEDevice::setPower(ESP_PWR_LVL_P9);
    NimBLEDevice::setSecurityAuth(true, false, true);          // bond, no passkey, secure connections
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
    NimBLEScan* s = NimBLEDevice::getScan();
    s->setAdvertisedDeviceCallbacks(new Seen(), true);
    s->setInterval(160);        // 100 ms
    s->setWindow(48);           // 30 ms of it: the rest is Wi-Fi's
    s->setMaxResults(0);
    inited = true;
}

void connect_to(const std::string& addr, uint8_t type) {
    if (!client) {
        client = NimBLEDevice::createClient();
        client->setConnectTimeout(8);
    }
    logf("bt: connecting to %s", addr.c_str());
    if (!client->connect(NimBLEAddress(addr, type))) {
        logf("bt: connect failed");
        return;
    }
    if (!client->secureConnection()) {
        logf("bt: it refused to pair");
        client->disconnect();
        return;
    }
    int n = 0;
    NimBLERemoteService* hid = client->getService(NimBLEUUID(HID_SERVICE));
    if (hid) {
        auto* chars = hid->getCharacteristics(true);
        if (chars)
            for (auto* c : *chars)
                if (c->getUUID() == NimBLEUUID(HID_REPORT) && c->canNotify() && c->subscribe(true, on_report)) n++;
    }
    if (!n) {
        logf("bt: no HID input reports on it");
        client->disconnect();
        return;
    }
    nlasts = 0;
    connected = true;
    logf("bt: connected to %s, %d input reports", addr.c_str(), n);
}

void task(void*) {
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(200));
        if (!inited) continue;
        if (connected && !(client && client->isConnected())) {
            connected = false;
            xSemaphoreTake(mu, portMAX_DELAY);
            held_name.clear();
            xSemaphoreGive(mu);
            logf("bt: disconnected");
        }
        if (pairing && (int32_t)(millis() - pair_until) >= 0) {
            pairing = false;
            NimBLEDevice::getScan()->stop();
            logf("bt: no remote in pairing mode found");
        }
        if (want_connect) {
            bool was_pairing = pairing;
            std::string addr = cand_addr, nm = cand_name;
            uint8_t type = cand_type;
            connect_to(addr, type);
            want_connect = false;
            if (connected && was_pairing) {
                xSemaphoreTake(mu, portMAX_DELAY);
                saved_addr = addr;
                saved_name = nm;
                saved_type = type;
                xSemaphoreGive(mu);
                prefs.putString("addr", addr.c_str());
                prefs.putString("name", nm.c_str());
                prefs.putUChar("type", type);
                pairing = false;
            }
            continue;
        }
        if (!connected && (pairing || !saved_addr.empty())) {
            NimBLEScan* s = NimBLEDevice::getScan();
            if (!s->isScanning()) {
                s->setActiveScan(pairing);
                s->start(0, scan_done, false);
            }
        }
    }
}

}  // namespace

namespace bt {

void begin(std::function<void(const char*)> log) {
    logger = log;
    mu = xSemaphoreCreateMutex();
    keys = xQueueCreate(16, 20);
    prefs.begin("rook-bt", false);
    saved_addr = prefs.getString("addr", "").c_str();
    saved_name = prefs.getString("name", "").c_str();
    saved_type = prefs.getUChar("type", 0);
    if (!saved_addr.empty()) radio_on();
    xTaskCreatePinnedToCore(task, "bt", 8192, nullptr, 1, nullptr, 0);
}

void pair(uint32_t seconds) {
    radio_on();
    if (client && client->isConnected()) client->disconnect();
    NimBLEDevice::getScan()->stop();
    pair_until = millis() + seconds * 1000;
    pairing = true;
    logf("bt: looking for a remote in pairing mode");
}

void forget() {
    pairing = false;
    if (inited) {
        NimBLEDevice::getScan()->stop();
        if (client && client->isConnected()) client->disconnect();
        NimBLEDevice::deleteAllBonds();
    }
    xSemaphoreTake(mu, portMAX_DELAY);
    saved_addr.clear();
    saved_name.clear();
    xSemaphoreGive(mu);
    prefs.remove("addr");
    prefs.remove("name");
    prefs.remove("type");
}

bool saved() { return !saved_addr.empty(); }

std::string state() {
    if (!inited) return "off";
    if (connected) return "connected";
    if (pairing) return "pairing";
    return saved_addr.empty() ? "idle" : "waiting";
}

std::string name() {
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string n = saved_name;
    xSemaphoreGive(mu);
    return n;
}

std::string address() {
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string a = saved_addr;
    xSemaphoreGive(mu);
    return a;
}

std::string last_report() {
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string h = last_hex;
    xSemaphoreGive(mu);
    return h;
}

std::string held() {
    if (!mu) return "";
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string h = held_name;
    xSemaphoreGive(mu);
    return h;
}

bool next_key(std::string& name) {
    char b[20];
    if (!keys || xQueueReceive(keys, b, 0) != pdTRUE) return false;
    name = b;
    return true;
}

}  // namespace bt

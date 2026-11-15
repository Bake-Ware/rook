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

// Voice (ATVV 0.4): commands are written to TX, audio arrives on RX, the
// remote's side of the conversation on CTL.
const char* const V_SVC = "ab5e0001-5a21-4f05-bc7d-af01f617b664";
const char* const V_TX = "ab5e0002-5a21-4f05-bc7d-af01f617b664";
const char* const V_RX = "ab5e0003-5a21-4f05-bc7d-af01f617b664";
const char* const V_CTL = "ab5e0004-5a21-4f05-bc7d-af01f617b664";
const uint32_t V_MAX_MS = 12000;            // an utterance is cut off here
const size_t V_BUF_MAX = 16000;             // nobody is taking the audio: stop keeping it
NimBLERemoteCharacteristic* v_tx = nullptr;
volatile bool v_ready = false, v_open = false, v_want_open = false, v_want_close = false, v_done = false;
volatile uint8_t v_codec = 1;
uint32_t v_opened = 0;
std::string v_buf, v_caps, v_ctl, services_text;

void on_audio(NimBLERemoteCharacteristic*, uint8_t* d, size_t len, bool) {
    if (!v_open) return;
    xSemaphoreTake(mu, portMAX_DELAY);
    if (v_buf.size() + len <= V_BUF_MAX) v_buf.append((const char*)d, len);
    xSemaphoreGive(mu);
}

void logf(const char* fmt, ...);

// Runs on the Bluetooth stack's task: only flags here, the writes are the bt task's.
void on_ctl(NimBLERemoteCharacteristic*, uint8_t* d, size_t len, bool) {
    char hex[40];
    int at = 0;
    for (size_t i = 0; i < len && i < 16; i++) at += snprintf(hex + at, sizeof hex - at, "%02x", d[i]);
    logf("bt: voice control %s", hex);
    xSemaphoreTake(mu, portMAX_DELAY);
    v_ctl = hex;
    if (len && d[0] == 0x0B) v_caps = hex;
    xSemaphoreGive(mu);
    if (!len) return;
    if (d[0] == 0x08) {                      // START_SEARCH: its Assistant key
        v_want_open = true;
    } else if (d[0] == 0x0B && len >= 5) {   // CAPS_RESP: version, codecs
        v_codec = (d[4] & 0x02) ? 2 : 1;
    } else if ((d[0] == 0x00 || d[0] == 0x0C) && v_open) {   // AUDIO_STOP, MIC_OPEN_ERROR
        v_open = false;
        v_done = true;
    }
}

void logf(const char* fmt, ...);

void logf(const char* fmt, ...) {
    char b[200];
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
    std::string svcs;
    auto* all = client->getServices(true);
    if (all)
        for (auto* s : *all) svcs += (svcs.empty() ? "" : " ") + s->getUUID().toString();
    v_tx = nullptr;
    bool voice = false;
    NimBLERemoteService* vs = client->getService(NimBLEUUID(V_SVC));
    if (vs) {
        NimBLERemoteCharacteristic* rx = vs->getCharacteristic(NimBLEUUID(V_RX));
        NimBLERemoteCharacteristic* ctl = vs->getCharacteristic(NimBLEUUID(V_CTL));
        v_tx = vs->getCharacteristic(NimBLEUUID(V_TX));
        if (v_tx && rx && ctl && rx->subscribe(true, on_audio) && ctl->subscribe(true, on_ctl)) {
            const uint8_t caps[] = {0x0A, 0x00, 0x04, 0x00, 0x03};      // GET_CAPS: 0.4, ADPCM 8 and 16 kHz
            voice = v_tx->writeValue(caps, sizeof caps, !v_tx->canWriteNoResponse());
        }
    }
    xSemaphoreTake(mu, portMAX_DELAY);
    services_text = svcs;
    v_buf.clear();
    xSemaphoreGive(mu);
    v_open = v_want_open = v_want_close = v_done = false;
    v_ready = voice;
    connected = true;
    logf("bt: connected to %s, %d input reports, voice %s (mtu %d)", addr.c_str(), n, voice ? "yes" : "no",
         (int)client->getMTU());
    logf("bt: services %s", svcs.c_str());
}

void task(void*) {
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(v_ready ? 30 : 200));
        if (!inited) continue;
        if (connected && v_ready && v_tx && client && client->isConnected()) {
            bool rsp = !v_tx->canWriteNoResponse();
            if (v_want_open) {
                v_want_open = false;
                if (!v_open) {
                    xSemaphoreTake(mu, portMAX_DELAY);
                    v_buf.clear();
                    xSemaphoreGive(mu);
                    const uint8_t open[] = {0x0C, 0x00, v_codec};        // MIC_OPEN, codec
                    v_done = false;
                    v_opened = millis();
                    v_open = true;                                        // before the write: audio may beat its answer
                    if (!v_tx->writeValue(open, sizeof open, rsp)) {
                        v_open = false;
                        logf("bt: the remote did not take MIC_OPEN");
                    }
                }
            }
            if (v_open && (v_want_close || millis() - v_opened > V_MAX_MS)) {
                const uint8_t close[] = {0x0D};                           // MIC_CLOSE
                v_tx->writeValue(close, sizeof close, rsp);
                v_open = false;
                v_done = true;
            }
            v_want_close = false;
        }
        if (connected && !(client && client->isConnected())) {
            connected = false;
            v_ready = false;
            if (v_open) {
                v_open = false;
                v_done = true;
            }
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

bool voice_ready() { return connected && v_ready; }

bool voice_open() { return v_open; }

void voice_start() {
    if (voice_ready() && !v_open) v_want_open = true;
}

void voice_stop() {
    if (v_open) v_want_close = true;
}

int voice_codec() { return v_codec; }

bool voice_take(std::string& out, bool& done, size_t min_bytes) {
    out.clear();
    done = false;
    if (!mu) return false;
    bool ended = v_done && !v_open;
    xSemaphoreTake(mu, portMAX_DELAY);
    if (v_buf.size() >= min_bytes || (ended && !v_buf.empty())) out.swap(v_buf);
    xSemaphoreGive(mu);
    if (ended) {
        v_done = false;
        done = true;
    }
    return done || !out.empty();
}

std::string services() {
    if (!mu) return "";
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string s = services_text;
    xSemaphoreGive(mu);
    return s;
}

std::string voice_info() {
    if (!mu) return "";
    xSemaphoreTake(mu, portMAX_DELAY);
    std::string s = "caps " + (v_caps.empty() ? std::string("-") : v_caps) + ", last " + (v_ctl.empty() ? std::string("-") : v_ctl);
    xSemaphoreGive(mu);
    return s;
}

bool next_key(std::string& name) {
    char b[20];
    if (!keys || xQueueReceive(keys, b, 0) != pdTRUE) return false;
    name = b;
    return true;
}

}  // namespace bt

#include "ota.h"
#include <HTTPClient.h>
#include <Update.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_ota_ops.h>
#include <mbedtls/sha256.h>

// The Arduino core would confirm a new build the moment it boots; this one
// confirms itself only once it has reached the hub (ota::tick).
extern "C" bool verifyRollbackLater() { return true; }

namespace ota {

static const uint32_t TRIAL_MS = 5 * 60 * 1000;
static std::function<void(const char*)> log_;
static std::function<void(const std::string&)> show_;
static SemaphoreHandle_t mu;
static std::string state_ = "idle", error_, url_, sha_;
static volatile int percent_ = 0;
static volatile bool busy_ = false, trial_ = false;

static void say(const char* fmt, ...) {
    char b[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    if (log_) log_(b);
}

static void set_state(const std::string& s, const std::string& err = "") {
    xSemaphoreTake(mu, portMAX_DELAY);
    state_ = s;
    error_ = err;
    xSemaphoreGive(mu);
}

void begin(std::function<void(const char*)> log, std::function<void(const std::string&)> show) {
    log_ = log;
    show_ = show;
    mu = xSemaphoreCreateMutex();
    esp_ota_img_states_t st;
    const esp_partition_t* run = esp_ota_get_running_partition();
    if (run && esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        trial_ = true;
        say("update: this build is on trial; it is kept once the hub is reached");
    }
}

void confirm() {
    if (!trial_) return;
    trial_ = false;
    esp_ota_mark_app_valid_cancel_rollback();
    say("update: build kept");
}

void tick(bool hub_ok) {
    if (!trial_) return;
    if (hub_ok) {
        confirm();
    } else if (millis() > TRIAL_MS) {
        say("update: no hub in %u s, going back to the build before", (unsigned)(TRIAL_MS / 1000));
        if (show_) show_("Update\nfailed:\ngoing\nback");
        delay(1500);
        esp_ota_mark_app_invalid_rollback_and_reboot();
    }
}

bool busy() { return busy_; }

static std::string hex(const uint8_t* b, size_t n) {
    static const char* d = "0123456789abcdef";
    std::string s;
    for (size_t i = 0; i < n; i++) {
        s += d[b[i] >> 4];
        s += d[b[i] & 15];
    }
    return s;
}

// Download into the spare slot. Blank on success, else what went wrong.
static std::string pull(const std::string& url, const std::string& want) {
    bool tls = url.rfind("https://", 0) == 0;
    WiFiClient plain;
    WiFiClientSecure secure;
    secure.setInsecure();              // the hash below is what vouches for the build
    HTTPClient http;
    http.setConnectTimeout(8000);
    http.setTimeout(8000);
    http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
    bool begun = tls ? http.begin(secure, url.c_str()) : http.begin(plain, url.c_str());
    if (!begun) return "bad url";
    int code = http.GET();
    if (code != 200) {
        http.end();
        return code < 0 ? std::string("download failed: ") + http.errorToString(code).c_str()
                        : "download failed: http " + std::to_string(code);
    }
    int size = http.getSize();
    if (size <= 0) {
        http.end();
        return "the server gave no length for the file";
    }
    const esp_partition_t* next = esp_ota_get_next_update_partition(nullptr);
    if (!next) {
        http.end();
        return "no spare app slot on this flash layout";
    }
    if ((size_t)size > next->size) {
        http.end();
        return "the build (" + std::to_string(size) + " bytes) is bigger than the spare slot (" +
               std::to_string((unsigned)next->size) + ")";
    }
    if (!Update.begin(size)) {
        http.end();
        return std::string("can't start: ") + Update.errorString();
    }
    mbedtls_sha256_context sha;
    mbedtls_sha256_init(&sha);
    mbedtls_sha256_starts_ret(&sha, 0);
    WiFiClient* in = http.getStreamPtr();
    static uint8_t buf[2048];
    int got = 0, shown = -1;
    uint32_t quiet = millis();
    std::string err;
    while (got < size) {
        int n = in->available();
        if (n <= 0) {
            if (!http.connected() && in->available() <= 0) { err = "the download broke off"; break; }
            if (millis() - quiet > 15000) { err = "the download stalled"; break; }
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }
        n = in->read(buf, std::min((int)sizeof buf, std::min(n, size - got)));
        if (n <= 0) continue;
        quiet = millis();
        mbedtls_sha256_update_ret(&sha, buf, n);
        if (Update.write(buf, n) != (size_t)n) { err = std::string("flash write failed: ") + Update.errorString(); break; }
        got += n;
        percent_ = (int)((int64_t)got * 100 / size);
        if (percent_ / 5 != shown) {
            shown = percent_ / 5;
            if (show_) show_("Update\n" + std::to_string((int)percent_) + "%");
        }
        vTaskDelay(1);
    }
    http.end();
    uint8_t digest[32];
    mbedtls_sha256_finish_ret(&sha, digest);
    mbedtls_sha256_free(&sha);
    if (err.empty() && hex(digest, 32) != want) err = "the file's SHA-256 is " + hex(digest, 32) + ", not the one given";
    if (!err.empty()) {
        Update.abort();
        return err;
    }
    if (!Update.end(true)) return std::string("can't finish: ") + Update.errorString();
    return "";
}

static void task(void*) {
    std::string url, sha;
    xSemaphoreTake(mu, portMAX_DELAY);
    url = url_;
    sha = sha_;
    xSemaphoreGive(mu);
    say("update: pulling %s", url.c_str());
    if (show_) show_("Update\n0%");
    std::string err = pull(url, sha);
    if (!err.empty()) {
        say("update: %s", err.c_str());
        set_state("failed", err);
        if (show_) show_("Update\nfailed");
        busy_ = false;
        vTaskDelete(nullptr);
        return;
    }
    set_state("restarting");
    say("update: written and checked; restarting into it");
    if (show_) show_("Update\ndone:\nrestart");
    vTaskDelay(pdMS_TO_TICKS(2500));   // let the reply and the log line out
    ESP.restart();
}

bool start(const std::string& url, const std::string& sha256, std::string& err) {
    std::string sha;
    for (char c : sha256) sha += (char)tolower((unsigned char)c);
    bool hexok = sha.size() == 64;
    for (char c : sha) hexok = hexok && isxdigit((unsigned char)c);
    if (!hexok) { err = "bad args: sha256 is the build's SHA-256, 64 hex characters"; return false; }
    if (url.rfind("http://", 0) != 0 && url.rfind("https://", 0) != 0) { err = "bad args: url must be http or https"; return false; }
    if (WiFi.status() != WL_CONNECTED) { err = "no Wi-Fi"; return false; }
    if (busy_) { err = "an update is already running"; return false; }
    if (trial_) { err = "this build is still on trial; try again once it has reached the hub"; return false; }
    busy_ = true;
    percent_ = 0;
    xSemaphoreTake(mu, portMAX_DELAY);
    url_ = url;
    sha_ = sha;
    state_ = "downloading";
    error_.clear();
    xSemaphoreGive(mu);
    if (xTaskCreate(task, "ota", 12288, nullptr, 1, nullptr) != pdPASS) {
        busy_ = false;
        set_state("failed", "out of memory");
        err = "out of memory";
        return false;
    }
    return true;
}

void fill(JsonObject o) {
    xSemaphoreTake(mu, portMAX_DELAY);
    o["state"] = state_;
    if (!error_.empty()) o["error"] = error_;
    xSemaphoreGive(mu);
    if (busy_) o["percent"] = (int)percent_;
    o["trial"] = (bool)trial_;
    const esp_partition_t* run = esp_ota_get_running_partition();
    const esp_partition_t* next = esp_ota_get_next_update_partition(nullptr);
    if (run) o["slot"] = run->label;
    if (next) o["room"] = (unsigned)next->size;
}

}  // namespace ota

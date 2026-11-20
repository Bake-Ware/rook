#include "ota.h"
#include <HTTPClient.h>
#include <Update.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <esp_ota_ops.h>
#include <mbedtls/sha256.h>
#include <memory>

// The Arduino core would confirm a new build the moment it boots; this one
// confirms itself only once it has settled or reached the hub (ota::tick).
extern "C" bool verifyRollbackLater() { return true; }

namespace ota {

static const uint32_t TRIAL_MS = 5 * 60 * 1000;
static const uint32_t SETTLE_MS = 20000;       // booted this long (and settled) before a build is kept without the hub
static const uint32_t STALL_MS = 20000;        // no byte for this long: the connection is given up and tried again
static const int TRIES = 5;                    // a download, in all
static const size_t FEED_MAX = 1024;           // the feed is a few lines
static std::function<void(const char*)> log_;
static std::function<void(const std::string&)> show_;
static std::function<void(bool)> quiet_;
static SemaphoreHandle_t mu;
static std::string state_ = "idle", error_, url_, sha_, board_, version_, fallback_;   // under mu
static volatile int percent_ = 0;
static volatile bool busy_ = false, trial_ = false, want_check_ = false, fell_back_ = false;
// How the last download went, for fill(): tries made, the least free heap seen
// while it ran, and free heap and largest block when it failed (0: it did not).
static volatile int tries_ = 0;
static volatile uint32_t heap_min_ = 0, fail_heap_ = 0, fail_block_ = 0;

static void say(const char* fmt, ...) {
    char b[200];
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

void begin(std::function<void(const char*)> log, std::function<void(const std::string&)> show,
           std::function<void(bool)> quiet) {
    log_ = log;
    show_ = show;
    quiet_ = quiet;
    mu = xSemaphoreCreateMutex();
    esp_ota_img_states_t st;
    const esp_partition_t* run = esp_ota_get_running_partition();
    if (run && esp_ota_get_state_partition(run, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY) {
        trial_ = true;
        say("update: this build is on trial; it is kept once it has settled or the hub is reached");
    }
}

void confirm() {
    if (!trial_) return;
    trial_ = false;
    esp_ota_mark_app_valid_cancel_rollback();
    say("update: build kept");
}

void tick(bool hub_ok, bool settled) {
    if (!trial_) return;
    if (hub_ok) {
        say("update: the hub is reached");
        confirm();
    } else if (settled && millis() > SETTLE_MS) {
        say("update: settled (on Wi-Fi, its remote back)");
        confirm();
    } else if (millis() > TRIAL_MS) {
        say("update: not settled and no hub in %u s, going back to the build before", (unsigned)(TRIAL_MS / 1000));
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

static void note_heap() {
    uint32_t h = ESP.getFreeHeap();
    if (!heap_min_ || h < heap_min_) heap_min_ = h;
}

// One GET of url, from byte got on. The client objects live only as long as the
// request: a TLS one (several KB of heap) only for https.
struct Get {
    WiFiClient plain;
    std::unique_ptr<WiFiClientSecure> secure;
    HTTPClient http;
    int code = 0;
    bool begin(const std::string& url, int from) {
        bool tls = url.rfind("https://", 0) == 0;
        if (tls) {
            secure.reset(new WiFiClientSecure);
            secure->setInsecure();     // the hash is what vouches for the build
        }
        http.setConnectTimeout(8000);
        http.setTimeout(8000);
        http.setReuse(false);
        http.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
        bool begun = tls ? http.begin(*secure, url.c_str()) : http.begin(plain, url.c_str());
        if (!begun) return false;
        static const char* keys[] = {"Content-Range"};
        http.collectHeaders(keys, 1);
        if (from > 0) http.addHeader("Range", ("bytes=" + std::to_string(from) + "-").c_str());
        code = http.GET();
        return true;
    }
};

// Where a download is: the bytes had (written and hashed), the build's whole size.
struct Pull {
    std::string url;
    int got = 0, size = -1;
    bool begun = false;
    mbedtls_sha256_context sha;
};

// One try at the rest of p. Blank: it is all here. retry: worth another try.
static std::string pull_once(Pull& p, bool& retry) {
    static uint8_t buf[2048];
    retry = false;
    Get g;
    if (!g.begin(p.url, p.got)) return "bad url";
    int code = g.code;
    if (code < 0) {
        retry = true;
        return std::string("download failed: ") + g.http.errorToString(code).c_str();
    }
    int skip = 0;                       // bytes to read past (a server that answered the whole file again)
    if (p.got > 0 && code == 206) {
        String cr = g.http.header("Content-Range");    // bytes <from>-<to>/<size>
        long from = -1;
        if (cr.startsWith("bytes ")) from = atol(cr.c_str() + 6);
        if (from != p.got) {
            say("update: the server resumed at %ld, not %d: from the start", from, p.got);
            return "the server resumed at the wrong place";
        }
    } else if (code == 200 && p.got > 0) {
        skip = p.got;                   // no Range support: the start comes again
        if (g.http.getSize() > 0 && g.http.getSize() != p.size) return "the file changed while it was downloading";
    } else if (code != 200) {
        retry = code >= 500;
        return "download failed: http " + std::to_string(code);
    }
    if (!p.begun) {
        int size = g.http.getSize();
        if (size <= 0) return "the server gave no length for the file";
        const esp_partition_t* next = esp_ota_get_next_update_partition(nullptr);
        if (!next) return "no spare app slot on this flash layout";
        if ((size_t)size > next->size)
            return "the build (" + std::to_string(size) + " bytes) is bigger than the spare slot (" +
                   std::to_string((unsigned)next->size) + ")";
        if (!Update.begin(size)) return std::string("can't start: ") + Update.errorString();
        p.size = size;
        p.begun = true;
    }
    WiFiClient* in = g.http.getStreamPtr();
    int shown = percent_ / 5;
    uint32_t quiet = millis();
    note_heap();
    while (p.got < p.size) {
        int n = in->available();
        if (n <= 0) {
            if (!g.http.connected() && in->available() <= 0) { retry = true; return "the download broke off"; }
            if (millis() - quiet > STALL_MS) { retry = true; return "the download stalled"; }
            vTaskDelay(pdMS_TO_TICKS(5));
            continue;
        }
        int want = skip ? skip : p.size - p.got;
        n = in->read(buf, std::min((int)sizeof buf, std::min(n, want)));
        if (n <= 0) continue;
        quiet = millis();
        if (skip) {
            skip -= n;
            continue;
        }
        mbedtls_sha256_update_ret(&p.sha, buf, n);
        if (Update.write(buf, n) != (size_t)n) return std::string("flash write failed: ") + Update.errorString();
        p.got += n;
        percent_ = (int)((int64_t)p.got * 100 / p.size);
        if (percent_ / 5 != shown) {
            shown = percent_ / 5;
            note_heap();
            if (show_) show_("Update\n" + std::to_string((int)percent_) + "%");
        }
        vTaskDelay(1);
    }
    return "";
}

// Download into the spare slot, trying again (from where it got to) when the
// connection stalls or breaks. Blank on success, else what went wrong.
static std::string pull(const std::string& url, const std::string& want) {
    Pull p;
    p.url = url;
    mbedtls_sha256_init(&p.sha);
    mbedtls_sha256_starts_ret(&p.sha, 0);
    tries_ = 0;
    heap_min_ = fail_heap_ = fail_block_ = 0;
    std::string err;
    for (int t = 0; t < TRIES; t++) {
        if (t) {
            say("update: %s at %d of %d bytes (heap %u, largest block %u); trying again", err.c_str(), p.got, p.size,
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMaxAllocHeap());
            if (show_) show_("Update\nretrying");
            vTaskDelay(pdMS_TO_TICKS(1500 * t));
            for (int w = 0; w < 30 && WiFi.status() != WL_CONNECTED; w++) vTaskDelay(pdMS_TO_TICKS(500));
        }
        tries_ = t + 1;
        bool retry = false;
        err = pull_once(p, retry);
        if (err.empty() || !retry) break;
    }
    if (!err.empty()) {
        fail_heap_ = ESP.getFreeHeap();
        fail_block_ = ESP.getMaxAllocHeap();
    }
    uint8_t digest[32];
    mbedtls_sha256_finish_ret(&p.sha, digest);
    mbedtls_sha256_free(&p.sha);
    if (err.empty() && hex(digest, 32) != want) err = "the file's SHA-256 is " + hex(digest, 32) + ", not the one given";
    if (!err.empty()) {
        if (p.begun) Update.abort();
        return err;
    }
    if (!Update.end(true)) return std::string("can't finish: ") + Update.errorString();
    return "";
}

// "0.12.2" against "0.12.1": numbers part by part. True: a is newer.
static bool newer(const std::string& a, const std::string& b) {
    const char *x = a.c_str(), *y = b.c_str();
    while (*x || *y) {
        long u = strtol(x, (char**)&x, 10), v = strtol(y, (char**)&y, 10);
        if (u != v) return u > v;
        if (*x == '.') x++;
        else if (*x) break;
        if (*y == '.') y++;
        else if (*y) break;
    }
    return false;
}

// The feed: what the newest build for this board is. Blank: url and sha256 are
// set (whether or not it is newer: version says). Else why it could not be had.
static std::string read_feed(const std::string& board, std::string& version, std::string& moniker, std::string& url,
                             std::string& sha) {
    std::string feed = std::string(FEED_BASE) + "latest-" + board + ".json";
    std::string body;
    {
        Get g;
        if (!g.begin(feed, 0)) return "bad feed url";
        if (g.code != 200)
            return g.code < 0 ? std::string("feed: ") + g.http.errorToString(g.code).c_str()
                              : "feed: http " + std::to_string(g.code);
        int size = g.http.getSize();
        if (size > (int)FEED_MAX) return "feed: too big";
        WiFiClient* in = g.http.getStreamPtr();
        uint32_t t0 = millis();
        while (body.size() < FEED_MAX && (size < 0 || (int)body.size() < size) && millis() - t0 < 8000) {
            int c = in->read();
            if (c < 0) {
                if (!g.http.connected()) break;
                vTaskDelay(pdMS_TO_TICKS(5));
                continue;
            }
            body += (char)c;
        }
    }
    JsonDocument d;
    if (deserializeJson(d, body)) return "feed: not JSON";
    version = d["version"] | "";
    moniker = d["moniker"] | "";
    url = d["url"] | "";
    sha = d["sha256"] | "";
    if (version.empty() || url.empty() || sha.size() != 64) return "feed: version, url and sha256 are needed";
    if (url.rfind("http://", 0) != 0 && url.rfind("https://", 0) != 0) url = std::string(FEED_BASE) + url;   // next to the feed
    return "";
}

static void done_failed(const std::string& err) {
    say("update: %s", err.c_str());
    set_state("failed", err);
    if (show_) show_("Update\nfailed");
    vTaskDelay(pdMS_TO_TICKS(5000));
    if (show_) show_("");                // back to the status screen
}

static void task(void*) {
    std::string url, sha, board, version;
    xSemaphoreTake(mu, portMAX_DELAY);
    url = url_;
    sha = sha_;
    board = board_;
    version = version_;
    xSemaphoreGive(mu);
    if (want_check_) {
        want_check_ = false;
        std::string nv, moniker;
        std::string err = read_feed(board, nv, moniker, url, sha);
        if (!err.empty()) {
            say("update: no feed (%s); asking the hub", err.c_str());
            xSemaphoreTake(mu, portMAX_DELAY);
            fallback_ = err;
            state_ = "idle";
            error_ = err;
            xSemaphoreGive(mu);
            fell_back_ = true;
            busy_ = false;
            vTaskDelete(nullptr);
            return;
        }
        if (!newer(nv, version)) {
            say("update: %s is the newest (the feed has %s %s)", version.c_str(), nv.c_str(), moniker.c_str());
            set_state("newest");
            if (show_) show_("Update\nnewest");
            vTaskDelay(pdMS_TO_TICKS(3000));
            if (show_) show_("");
            busy_ = false;
            vTaskDelete(nullptr);
            return;
        }
        say("update: the feed has %s (%s), newer than %s", nv.c_str(), moniker.c_str(), version.c_str());
        for (auto& c : sha) c = (char)tolower((unsigned char)c);
        set_state("downloading");
    }
    say("update: pulling %s (heap %u, largest block %u)", url.c_str(), (unsigned)ESP.getFreeHeap(),
        (unsigned)ESP.getMaxAllocHeap());
    if (show_) show_("Update\n0%");
    if (quiet_) quiet_(true);
    std::string err = pull(url, sha);
    if (quiet_) quiet_(false);
    if (!err.empty()) {
        done_failed(err);
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

static bool can_run(std::string& err) {
    if (WiFi.status() != WL_CONNECTED) { err = "no Wi-Fi"; return false; }
    if (busy_) { err = "an update is already running"; return false; }
    if (trial_) { err = "this build is still on trial; try again once it has settled or reached the hub"; return false; }
    return true;
}

// The task's stack comes out of the heap, and the C3 bridge has little of it
// in one piece: 8 KB is what the download needs, and the rest is left to the
// Wi-Fi and TCP buffers the transfer itself lives on.
static bool spawn(std::string& err) {
    if (xTaskCreate(task, "ota", 8192, nullptr, 1, nullptr) != pdPASS) {
        busy_ = false;
        want_check_ = false;
        set_state("failed", "out of memory");
        err = "out of memory";
        return false;
    }
    return true;
}

bool start(const std::string& url, const std::string& sha256, std::string& err) {
    std::string sha;
    for (char c : sha256) sha += (char)tolower((unsigned char)c);
    bool hexok = sha.size() == 64;
    for (char c : sha) hexok = hexok && isxdigit((unsigned char)c);
    if (!hexok) { err = "bad args: sha256 is the build's SHA-256, 64 hex characters"; return false; }
    if (url.rfind("http://", 0) != 0 && url.rfind("https://", 0) != 0) { err = "bad args: url must be http or https"; return false; }
    if (!can_run(err)) return false;
    busy_ = true;
    percent_ = 0;
    xSemaphoreTake(mu, portMAX_DELAY);
    url_ = url;
    sha_ = sha;
    state_ = "downloading";
    error_.clear();
    xSemaphoreGive(mu);
    return spawn(err);
}

bool check(const std::string& board, const std::string& version, std::string& err) {
    if (!can_run(err)) return false;
    busy_ = true;
    percent_ = 0;
    want_check_ = true;
    xSemaphoreTake(mu, portMAX_DELAY);
    board_ = board;
    version_ = version;
    state_ = "checking";
    error_.clear();
    xSemaphoreGive(mu);
    return spawn(err);
}

bool take_fallback(std::string& why) {
    if (!fell_back_) return false;
    fell_back_ = false;
    xSemaphoreTake(mu, portMAX_DELAY);
    why = fallback_;
    xSemaphoreGive(mu);
    return true;
}

void fill(JsonObject o) {
    xSemaphoreTake(mu, portMAX_DELAY);
    o["state"] = state_;
    if (!error_.empty()) o["error"] = error_;
    xSemaphoreGive(mu);
    if (busy_) o["percent"] = (int)percent_;
    o["trial"] = (bool)trial_;
    if (tries_) {
        o["tries"] = (int)tries_;
        if (heap_min_) o["heap_min"] = (unsigned)heap_min_;
    }
    if (fail_heap_) {
        o["failed_heap"] = (unsigned)fail_heap_;
        o["failed_block"] = (unsigned)fail_block_;
    }
    const esp_partition_t* run = esp_ota_get_running_partition();
    const esp_partition_t* next = esp_ota_get_next_update_partition(nullptr);
    if (run) o["slot"] = run->label;
    if (next) o["room"] = (unsigned)next->size;
}

}  // namespace ota

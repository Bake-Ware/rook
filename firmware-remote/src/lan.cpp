#include "lan.h"
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <algorithm>
#include <esp_random.h>
#include <lwip/etharp.h>
#include <lwip/netif.h>
#include <map>
#include <mdns.h>
#include "settings.h"

namespace lan {

static std::function<void(const char*)> log_;
static SemaphoreHandle_t mu;
static std::vector<Device> table;
static volatile uint32_t scans_ = 0;
static bool mdns_on = false;
static const size_t MAX_DEVICES = 24, MAX_KEPT = 8;

static void say(const char* fmt, ...) {
    char b[160];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    if (log_) log_(b);
}

static std::string lower(std::string s) {
    for (auto& c : s) c = (char)tolower((unsigned char)c);
    return s;
}

static std::string trim(const std::string& s) {
    size_t a = s.find_first_not_of(" \t\r\n"), b = s.find_last_not_of(" \t\r\n");
    return a == std::string::npos ? "" : s.substr(a, b - a + 1);
}

static bool has(const std::vector<std::string>& v, const std::string& s) {
    return std::find(v.begin(), v.end(), s) != v.end();
}

static void add_kind(Device& d, const std::string& k) {
    if (!has(d.kinds, k)) d.kinds.push_back(k);
}

std::string mac_of(const std::string& ip) {
    ip4_addr_t a;
    if (!netif_default || !ip4addr_aton(ip.c_str(), &a)) return "";
    for (int attempt = 0; attempt < 2; attempt++) {
        struct eth_addr* eth = nullptr;
        const ip4_addr_t* got = nullptr;
        if (etharp_find_addr(netif_default, &a, &eth, &got) >= 0 && eth) {
            char b[18];
            snprintf(b, sizeof b, "%02x:%02x:%02x:%02x:%02x:%02x", eth->addr[0], eth->addr[1], eth->addr[2],
                     eth->addr[3], eth->addr[4], eth->addr[5]);
            return b;
        }
        if (attempt == 0) {
            etharp_request(netif_default, &a);
            vTaskDelay(pdMS_TO_TICKS(200));
        }
    }
    return "";
}

static std::string id_of(const Device& d) {
    if (d.mac.empty()) return d.ip;
    std::string s;
    for (char c : d.mac) if (c != ':') s += c;
    return s;
}

// One header's value from an SSDP answer (name in lower case).
static std::string header(const std::string& msg, const char* name) {
    std::string low = lower(msg), key = std::string("\n") + name + ":";
    size_t p = low.find(key);
    if (p == std::string::npos) return "";
    p += key.size();
    size_t e = msg.find_first_of("\r\n", p);
    return trim(msg.substr(p, e == std::string::npos ? std::string::npos : e - p));
}

static std::string tag(const std::string& xml, const char* name) {
    std::string open = std::string("<") + name + ">", close = std::string("</") + name + ">";
    size_t a = xml.find(open);
    if (a == std::string::npos) return "";
    a += open.size();
    size_t b = xml.find(close, a);
    if (b == std::string::npos) return "";
    std::string v = trim(xml.substr(a, b - a));
    for (const char* ent : {"&amp;", "&apos;", "&quot;"}) {
        const char* to = !strcmp(ent, "&amp;") ? "&" : !strcmp(ent, "&apos;") ? "'" : "\"";
        for (size_t p; (p = v.find(ent)) != std::string::npos;) v.replace(p, strlen(ent), to);
    }
    return v;
}

static std::string http_get(const std::string& url, size_t max) {
    if (url.rfind("http://", 0) != 0) return "";
    WiFiClient c;
    HTTPClient http;
    http.setConnectTimeout(1500);
    http.setTimeout(2000);
    if (!http.begin(c, url.c_str())) return "";
    std::string body;
    if (http.GET() == 200) {
        WiFiClient* in = http.getStreamPtr();
        uint32_t end = millis() + 2500;
        while (body.size() < max && (int32_t)(end - millis()) > 0) {
            int n = in->available();
            if (n <= 0) {
                if (!http.connected()) break;
                vTaskDelay(pdMS_TO_TICKS(5));
                continue;
            }
            uint8_t buf[256];
            n = in->read(buf, std::min<int>(n, sizeof buf));
            if (n > 0) body.append((const char*)buf, n);
        }
    }
    http.end();
    return body;
}

static Device& at(std::vector<Device>& found, const std::string& ip) {
    for (auto& d : found)
        if (d.ip == ip) return d;
    found.emplace_back();
    found.back().ip = ip;
    return found.back();
}

static void ssdp_answer(std::vector<Device>& found, std::map<std::string, std::string>& where,
                        const std::string& ip, const std::string& msg) {
    std::string st = lower(header(msg, "st")), server = lower(header(msg, "server"));
    std::string loc = header(msg, "location");
    if (found.size() >= MAX_DEVICES) {
        bool known = false;
        for (auto& d : found) known = known || d.ip == ip;
        if (!known) return;
    }
    Device& d = at(found, ip);
    bool roku = st.find("roku:ecp") != std::string::npos || server.find("roku") != std::string::npos;
    bool webos = st.find("webos-second-screen") != std::string::npos || server.find("webos") != std::string::npos;
    if (roku) add_kind(d, "roku");
    if (webos) add_kind(d, "webos");
    if (st.find("dial-multiscreen") != std::string::npos) add_kind(d, "dial");
    if (st.find("samsung.com") != std::string::npos) add_kind(d, "samsung");
    if (d.kinds.empty()) add_kind(d, "upnp");
    if (st.find("roku:ecp") != std::string::npos) {
        size_t c = loc.find(':', 7);          // http://ip:port/
        d.port = c == std::string::npos ? 8060 : (uint16_t)atoi(loc.c_str() + c + 1);
    }
    // The description to read its name from: any, but not a Roku's ECP root.
    if (!loc.empty() && st.find("roku:ecp") == std::string::npos && !where.count(ip)) where[ip] = loc;
}

static void ssdp(std::vector<Device>& found, std::map<std::string, std::string>& where) {
    static const char* const STS[] = {"roku:ecp", "urn:lge-com:service:webos-second-screen:1",
                                      "urn:dial-multiscreen-org:service:dial:1",
                                      "urn:samsung.com:device:RemoteControlReceiver:1", "upnp:rootdevice"};
    WiFiUDP udp;
    if (!udp.begin(50000 + (esp_random() % 10000))) {
        say("scan: no UDP socket");
        return;
    }
    IPAddress group(239, 255, 255, 250);
    for (int round = 0; round < 2; round++) {
        for (const char* st : STS) {
            std::string m = std::string("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                                        "MAN: \"ssdp:discover\"\r\nMX: 2\r\nST: ") + st + "\r\n\r\n";
            udp.beginPacket(group, 1900);
            udp.write((const uint8_t*)m.data(), m.size());
            udp.endPacket();
            vTaskDelay(pdMS_TO_TICKS(30));
        }
        uint32_t end = millis() + (round == 0 ? 1200 : 2800);
        while ((int32_t)(end - millis()) > 0) {
            int n = udp.parsePacket();
            if (n <= 0) {
                vTaskDelay(pdMS_TO_TICKS(10));
                continue;
            }
            char buf[768];
            int r = udp.read(buf, sizeof buf - 1);
            if (r <= 0) continue;
            buf[r] = 0;
            ssdp_answer(found, where, udp.remoteIP().toString().c_str(), std::string("\n") + buf);
        }
    }
    udp.stop();
}

static void mdns(std::vector<Device>& found) {
    if (!mdns_on) {
        mdns_on = MDNS.begin(WiFi.getHostname());
        if (!mdns_on) {
            say("scan: mDNS would not start");
            return;
        }
    }
    static const struct { const char* svc; const char* kind; } SVCS[] = {
        {"_googlecast", "cast"}, {"_androidtvremote2", "androidtv"}, {"_airplay", "airplay"},
        {"_amzn-wplay", "firetv"}, {"_wled", "wled"}, {"_shelly", "shelly"},
        {"_esphomelib", "esphome"}, {"_hap", "homekit"}};
    for (auto& s : SVCS) {
        mdns_result_t* res = nullptr;
        if (mdns_query_ptr(s.svc, "_tcp", 1000, 12, &res) != ESP_OK || !res) continue;
        for (mdns_result_t* r = res; r; r = r->next) {
            std::string ip;
            for (mdns_ip_addr_t* a = r->addr; a; a = a->next) {
                if (a->addr.type != ESP_IPADDR_TYPE_V4) continue;
                ip = IPAddress(a->addr.u_addr.ip4.addr).toString().c_str();
                break;
            }
            if (ip.empty() || ip == "0.0.0.0") continue;
            if (found.size() >= MAX_DEVICES) {
                bool known = false;
                for (auto& d : found) known = known || d.ip == ip;
                if (!known) continue;
            }
            Device& d = at(found, ip);
            add_kind(d, s.kind);
            std::string name, model;
            for (size_t i = 0; i < r->txt_count; i++) {
                if (!r->txt[i].key || !r->txt[i].value) continue;
                std::string k = r->txt[i].key;
                if (k == "fn") name = r->txt[i].value;                    // a Chromecast's own name
                else if (k == "md" || k == "model") model = r->txt[i].value;
            }
            if (name.empty() && r->instance_name) name = r->instance_name;
            if (d.name.empty()) d.name = name;
            if (d.model.empty()) d.model = model;
        }
        mdns_query_results_free(res);
    }
}

static void save_kept() {
    JsonDocument doc;
    JsonArray a = doc.to<JsonArray>();
    for (auto& d : table) {
        if (!d.kept) continue;
        JsonObject o = a.add<JsonObject>();
        o["id"] = d.id;
        o["name"] = d.name;
        o["model"] = d.model;
        o["ip"] = d.ip;
        o["mac"] = d.mac;
        o["driver"] = d.driver;
        o["port"] = d.port;
        JsonArray k = o["kinds"].to<JsonArray>();
        for (auto& s : d.kinds) k.add(s);
    }
    std::string json;
    serializeJson(doc, json);
    if (json != settings::kv("lan_kept")) settings::set_kv("lan_kept", json);
}

void begin(std::function<void(const char*)> log) {
    log_ = log;
    mu = xSemaphoreCreateMutex();
    JsonDocument doc;
    if (deserializeJson(doc, settings::kv("lan_kept"))) return;
    for (JsonObjectConst o : doc.as<JsonArrayConst>()) {
        Device d;
        d.id = o["id"] | "";
        d.name = o["name"] | "";
        d.model = o["model"] | "";
        d.ip = o["ip"] | "";
        d.mac = o["mac"] | "";
        d.driver = o["driver"] | "";
        d.port = o["port"] | 0;
        for (JsonVariantConst k : o["kinds"].as<JsonArrayConst>()) d.kinds.push_back(k | "");
        d.kept = true;
        if (!d.id.empty()) table.push_back(d);
    }
}

int scan() {
    if (WiFi.status() != WL_CONNECTED) return 0;
    std::vector<Device> found;
    std::map<std::string, std::string> where;
    ssdp(found, where);
    mdns(found);
    for (auto& d : found) {
        d.seen = true;
        d.mac = mac_of(d.ip);
        if (has(d.kinds, "roku")) {
            d.driver = "roku";
            if (!d.port) d.port = 8060;
            std::string xml = http_get("http://" + d.ip + ":" + std::to_string(d.port) + "/query/device-info", 8192);
            std::string n = tag(xml, "user-device-name");
            if (n.empty()) n = tag(xml, "friendly-device-name");
            if (!n.empty()) d.name = n;
            n = tag(xml, "friendly-model-name");
            if (n.empty()) n = tag(xml, "model-name");
            if (!n.empty()) d.model = n;
        } else if (where.count(d.ip)) {
            std::string xml = http_get(where[d.ip], 6144);
            std::string n = tag(xml, "friendlyName");
            if (!n.empty()) d.name = n;
            n = tag(xml, "modelName");
            if (!n.empty() && d.model.empty()) d.model = n;
        }
        if (has(d.kinds, "webos")) d.driver = "webos";
        if (d.mac.empty()) d.mac = mac_of(d.ip);
        if (d.name.empty()) d.name = d.kinds[0] + " " + d.ip.substr(d.ip.rfind('.') + 1);
        d.id = id_of(d);
    }
    xSemaphoreTake(mu, portMAX_DELAY);
    for (auto& old : table) {
        if (!old.kept) continue;
        bool again = false;
        for (auto& d : found) {
            if (d.id != old.id) continue;
            d.kept = again = true;
        }
        if (!again) {                     // off or away: still offered, by what was last known
            old.seen = false;
            found.push_back(old);
        }
    }
    std::stable_sort(found.begin(), found.end(), [](const Device& x, const Device& y) {
        if (x.driver.empty() != y.driver.empty()) return !x.driver.empty();
        return lower(x.name) < lower(y.name);
    });
    table = found;
    save_kept();
    int n = 0;
    for (auto& d : table) n += d.seen;
    xSemaphoreGive(mu);
    scans_++;
    say("scan: %d devices answered", n);
    return n;
}

uint32_t scans() { return scans_; }

std::vector<Device> devices() {
    xSemaphoreTake(mu, portMAX_DELAY);
    std::vector<Device> t = table;
    xSemaphoreGive(mu);
    return t;
}

bool find(const std::string& id_or_name, Device& out) {
    std::string want = lower(trim(id_or_name));
    if (want.empty()) return false;
    auto t = devices();
    for (auto& d : t)
        if (lower(d.id) == want || d.ip == want || lower(d.mac) == want || lower(d.name) == want) { out = d; return true; }
    const Device* hit = nullptr;
    int hits = 0;
    for (auto& d : t)
        if (lower(d.name).find(want) != std::string::npos) { hit = &d; hits++; }
    if (hits != 1) return false;
    out = *hit;
    return true;
}

void keep(const std::string& id) {
    xSemaphoreTake(mu, portMAX_DELAY);
    size_t kept = 0;
    for (auto& d : table) kept += d.kept;
    for (auto& d : table)
        if (d.id == id && !d.kept && kept < MAX_KEPT) d.kept = true;
    save_kept();
    xSemaphoreGive(mu);
}

void forget(const std::string& id) {
    xSemaphoreTake(mu, portMAX_DELAY);
    for (auto it = table.begin(); it != table.end();) {
        if (it->id == id) {
            it->kept = false;
            if (!it->seen) { it = table.erase(it); continue; }
        }
        ++it;
    }
    save_kept();
    xSemaphoreGive(mu);
}

void learn_mac(const std::string& id) {
    xSemaphoreTake(mu, portMAX_DELAY);
    for (auto& d : table) {
        if (d.id != id || !d.mac.empty()) continue;
        d.mac = mac_of(d.ip);
    }
    save_kept();
    xSemaphoreGive(mu);
}

bool wake(const std::string& mac) {
    unsigned m[6];
    if (sscanf(mac.c_str(), "%x:%x:%x:%x:%x:%x", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5]) != 6) return false;
    uint8_t pkt[102];
    memset(pkt, 0xff, 6);
    for (int i = 0; i < 16; i++)
        for (int j = 0; j < 6; j++) pkt[6 + i * 6 + j] = (uint8_t)m[j];
    WiFiUDP udp;
    for (int i = 0; i < 3; i++) {
        for (IPAddress to : {IPAddress(255, 255, 255, 255), WiFi.broadcastIP()}) {
            udp.beginPacket(to, 9);
            udp.write(pkt, sizeof pkt);
            udp.endPacket();
        }
        vTaskDelay(pdMS_TO_TICKS(60));
    }
    udp.stop();
    return true;
}

void fill(const Device& d, JsonObject o) {
    o["id"] = d.id;
    o["name"] = d.name;
    if (!d.model.empty()) o["model"] = d.model;
    o["ip"] = d.ip;
    if (!d.mac.empty()) o["mac"] = d.mac;
    JsonArray k = o["kinds"].to<JsonArray>();
    for (auto& s : d.kinds) k.add(s);
    o["driver"] = d.driver;
    o["seen"] = d.seen;
    if (d.kept) o["kept"] = true;
}

}  // namespace lan

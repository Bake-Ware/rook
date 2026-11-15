#include "rc_canon.h"
#include "rc_util.h"
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <algorithm>
#include <vector>

namespace rc {

static const double MAX_SAFE = 9007199254740991.0;

// ECMAScript Number.prototype.toString for a finite, non-integral-or-huge double.
static bool number(double x, std::string& out) {
    if (!isfinite(x)) return false;
    if (x == 0) { out += '0'; return true; }
    std::string sign = x < 0 ? "-" : "";
    double ax = fabs(x);
    // Shortest round-trip digits. ArduinoJson 7.3+ keeps a parsed number with
    // few digits as a float, so a value that is exactly a float is printed with
    // the shortest digits that parse back to that float (what was on the wire).
    char buf[40];
    int prec = 0;
    bool is_float = (double)(float)ax == ax;
    for (prec = 0; prec < 17; prec++) {
        snprintf(buf, sizeof buf, "%.*e", prec, ax);
        if (is_float ? (strtof(buf, nullptr) == (float)ax) : (strtod(buf, nullptr) == ax)) break;
    }
    // buf = d[.ddd]e[+-]XX
    std::string s(buf);
    size_t epos = s.find('e');
    std::string mant = s.substr(0, epos);
    int e = atoi(s.c_str() + epos + 1);
    std::string digits;
    for (char c : mant) if (c != '.') digits += c;
    while (digits.size() > 1 && digits.back() == '0') digits.pop_back();
    int k = (int)digits.size();
    int n = e + 1;   // value = 0.d1..dk * 10^n
    std::string r;
    if (k <= n && n <= 21) {
        r = digits + std::string(n - k, '0');
    } else if (0 < n && n <= 21) {
        r = digits.substr(0, n) + "." + digits.substr(n);
    } else if (-6 < n && n <= 0) {
        r = "0." + std::string(-n, '0') + digits;
    } else {
        int ee = n - 1;
        r = digits.substr(0, 1);
        if (k > 1) r += "." + digits.substr(1);
        r += "e";
        r += ee >= 0 ? "+" : "-";
        r += std::to_string(ee < 0 ? -ee : ee);
    }
    out += sign + r;
    return true;
}

static void string(const char* s, std::string& out) {
    out += '"';
    for (const unsigned char* p = (const unsigned char*)s; *p; p++) {
        unsigned char c = *p;
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            case '\b': out += "\\b"; break;
            case '\f': out += "\\f"; break;
            default:
                if (c < 0x20) {
                    char b[8];
                    snprintf(b, sizeof b, "\\u%04x", c);
                    out += b;
                } else {
                    out += (char)c;
                }
        }
    }
    out += '"';
}

// UTF-8 -> UTF-16 code units, for key ordering.
static std::vector<uint16_t> utf16(const char* s) {
    std::vector<uint16_t> u;
    const unsigned char* p = (const unsigned char*)s;
    while (*p) {
        uint32_t cp;
        if (*p < 0x80) { cp = *p++; }
        else if ((*p & 0xE0) == 0xC0 && p[1]) { cp = (p[0] & 0x1F) << 6 | (p[1] & 0x3F); p += 2; }
        else if ((*p & 0xF0) == 0xE0 && p[1] && p[2]) { cp = (p[0] & 0x0F) << 12 | (p[1] & 0x3F) << 6 | (p[2] & 0x3F); p += 3; }
        else if ((*p & 0xF8) == 0xF0 && p[1] && p[2] && p[3]) {
            cp = (p[0] & 0x07) << 18 | (p[1] & 0x3F) << 12 | (p[2] & 0x3F) << 6 | (p[3] & 0x3F); p += 4;
        } else { cp = *p++; }
        if (cp >= 0x10000) {
            cp -= 0x10000;
            u.push_back((uint16_t)(0xD800 + (cp >> 10)));
            u.push_back((uint16_t)(0xDC00 + (cp & 0x3FF)));
        } else {
            u.push_back((uint16_t)cp);
        }
    }
    return u;
}

static bool canon(JsonVariantConst v, std::string& out, int depth) {
    if (depth > 32) return false;
    if (v.isNull()) { out += "null"; return true; }
    if (v.is<bool>()) { out += v.as<bool>() ? "true" : "false"; return true; }
    if (v.is<const char*>()) { string(v.as<const char*>(), out); return true; }
    if (v.is<JsonArrayConst>()) {
        out += '[';
        bool first = true;
        for (JsonVariantConst x : v.as<JsonArrayConst>()) {
            if (!first) out += ',';
            first = false;
            if (!canon(x, out, depth + 1)) return false;
        }
        out += ']';
        return true;
    }
    if (v.is<JsonObjectConst>()) {
        JsonObjectConst o = v.as<JsonObjectConst>();
        std::vector<std::pair<std::vector<uint16_t>, JsonPairConst>> kv;
        for (JsonPairConst p : o) kv.push_back({utf16(p.key().c_str()), p});
        std::sort(kv.begin(), kv.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
        out += '{';
        bool first = true;
        for (auto& e : kv) {
            if (!first) out += ',';
            first = false;
            string(e.second.key().c_str(), out);
            out += ':';
            if (!canon(e.second.value(), out, depth + 1)) return false;
        }
        out += '}';
        return true;
    }
    if (v.is<long long>()) {
        long long i = v.as<long long>();
        if (i > 9007199254740991LL || i < -9007199254740991LL) return false;
        out += std::to_string(i);
        return true;
    }
    if (v.is<unsigned long long>()) {
        unsigned long long i = v.as<unsigned long long>();
        if (i > 9007199254740991ULL) return false;
        out += std::to_string(i);
        return true;
    }
    if (v.is<double>()) {
        double d = v.as<double>();
        if (!isfinite(d)) return false;
        if (d == floor(d) && fabs(d) <= MAX_SAFE) {     // integral: integer form (-0 -> 0)
            long long i = (long long)d;
            out += std::to_string(i);
            return true;
        }
        return number(d, out);
    }
    return false;
}

bool canonical(JsonVariantConst v, std::string& out) {
    out.clear();
    return canon(v, out, 0);
}

bool args_hash(JsonVariantConst args, std::string& out) {
    std::string c;
    if (args.isNull()) c = "{}";
    else if (!canonical(args, c)) return false;
    uint8_t d[32];
    sha256(c.data(), c.size(), d);
    out = b64url_nopad(d, 32);
    return true;
}

static const char* prefix(const char* typ) {
    static const char* const P[][2] = {
        {"rook-ticket", "rook-ticket-v2\n"},       {"rook-lease", "rook-lease-v2\n"},
        {"rook-hubkey", "rook-hubkey-v2\n"},       {"rook-membership", "rook-membership-v2\n"},
        {"rook-announce", "rook-announce-v2\n"},   {"rook-plugin", "rook-plugin-v2\n"},
        {"rook-release", "rook-release-v2\n"},     {"rook-request", "rook-request-v2\n"},
    };
    if (!typ) return nullptr;
    for (auto& p : P) if (strcmp(p[0], typ) == 0) return p[1];
    return nullptr;
}

bool signing_input(JsonObjectConst obj, std::string& out) {
    const char* pre = prefix(obj["typ"].as<const char*>());
    if (!pre) return false;
    // canonical(obj minus sig/hubkey): emit the object ourselves, skipping them.
    std::vector<std::pair<std::vector<uint16_t>, JsonPairConst>> kv;
    for (JsonPairConst p : obj) {
        if (strcmp(p.key().c_str(), "sig") == 0 || strcmp(p.key().c_str(), "hubkey") == 0) continue;
        kv.push_back({utf16(p.key().c_str()), p});
    }
    std::sort(kv.begin(), kv.end(), [](const auto& a, const auto& b) { return a.first < b.first; });
    out = pre;
    out += '{';
    bool first = true;
    for (auto& e : kv) {
        if (!first) out += ',';
        first = false;
        string(e.second.key().c_str(), out);
        out += ':';
        if (!canon(e.second.value(), out, 1)) return false;
    }
    out += '}';
    return true;
}

}  // namespace rc

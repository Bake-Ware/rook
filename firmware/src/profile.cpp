#include "profile.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char* CORE = "2.0";
static const uint32_t HUB_GONE_MS = 90000;

void profileAnnounce(const Profile& p, JsonDocument& out) {
    out.clear();
    out["kind"] = "announce";
    out["worker_id"] = p.id;
    out["name"] = p.name;
    out["core"] = CORE;
    out["version"] = p.version;
    JsonObject m = out["manifests"].to<JsonArray>().add<JsonObject>();
    m["plugin"] = p.plugin;
    m["version"] = p.plugin_version;
    m["hash"] = p.manifest_hash;
    out["manifests_hash"] = p.manifests_hash;
}

void profileHeartbeat(const Profile& p, JsonDocument& out) {
    out.clear();
    out["kind"] = "heartbeat";
    out["worker_id"] = p.id;
    out["core"] = CORE;
    out["manifests_hash"] = p.manifests_hash;
}

static CapFn find(const Profile& p, const char* cap) {
    size_t n = strlen(p.plugin);
    if (strncmp(cap, p.plugin, n) != 0 || cap[n] != '.') return nullptr;
    for (const Cap* c = p.caps; c->name; c++)
        if (strcmp(c->name, cap + n + 1) == 0) return c->fn;
    return nullptr;
}

Heard profileHear(Profile& p, const uint8_t* payload, size_t len, uint32_t now_ms, JsonDocument& out) {
    // Only what the profile reads is kept: a call's ticket, an announce's
    // manifests and the rest are parsed past, never stored.
    JsonDocument filter;
    for (const char* k : {"kind", "name", "worker_id", "id", "cap", "target", "args"}) filter[k] = true;
    JsonDocument msg;
    if (deserializeJson(msg, payload, len, DeserializationOption::Filter(filter)) || !msg.is<JsonObject>())
        return HEARD_NOTHING;

    if (msg["kind"] == "announce" && msg["name"] == "rook" && msg["worker_id"].is<const char*>()) {
        const char* hub = msg["worker_id"];
        bool fresh = strncmp(hub, p.hub, sizeof(p.hub)) != 0 || now_ms - p.hub_heard_ms > HUB_GONE_MS;
        strncpy(p.hub, hub, sizeof(p.hub) - 1);
        p.hub[sizeof(p.hub) - 1] = '\0';
        p.hub_heard_ms = now_ms;
        return fresh ? HEARD_NEW_HUB : HEARD_NOTHING;
    }

    const char* cap = msg["cap"];
    const char* target = msg["target"];
    const char* id = msg["id"];
    if (!cap || !id || !target || strcmp(target, p.id) != 0) return HEARD_NOTHING;

    out.clear();
    out["id"] = id;
    out["from"] = p.id;
    CapFn fn = find(p, cap);
    if (!fn) {
        char error[160];
        snprintf(error, sizeof(error), "unknown capability: %s", cap);
        out["ok"] = false;
        out["error"] = error;
        return HEARD_REPLY;
    }
    const char* error = fn(msg["args"], out["result"].to<JsonObject>());
    out["ok"] = !error;
    if (error) {
        out.remove("result");
        out["error"] = error;
    }
    return HEARD_REPLY;
}

void reassemblerInit(Reassembler& r) {
    memset(&r, 0, sizeof(r));
}

static void drop(Reassembler::Slot& s) {
    free(s.buf);
    memset(&s, 0, sizeof(s));
}

size_t reassemble(Reassembler& r, const uint8_t* frag, size_t len, uint32_t now_ms, uint8_t** out) {
    if (len < FRAG_HEADER || frag[0] != 0x01) return 0;
    uint16_t seq = (uint16_t)(frag[17] << 8 | frag[18]);
    uint16_t total = (uint16_t)(frag[19] << 8 | frag[20]);
    const uint8_t* data = frag + FRAG_HEADER;
    size_t n = len - FRAG_HEADER;
    if (total == 0 || seq >= total || total > FRAG_MAX || n > FRAG_DATA) return 0;
    if (total == 1) {
        *out = (uint8_t*)malloc(n ? n : 1);
        if (!*out) return 0;
        memcpy(*out, data, n);
        return n;
    }

    Reassembler::Slot* slot = nullptr;
    Reassembler::Slot* spare = nullptr;       // a free slot, else the oldest message gives way
    uint32_t spare_age = 0;
    for (auto& s : r.slots) {
        if (s.buf && now_ms - s.started_ms > FRAG_TIMEOUT_MS) drop(s);
        uint32_t age = s.buf ? now_ms - s.started_ms : UINT32_MAX;
        if (s.buf && memcmp(s.fid, frag + 1, 16) == 0) {
            slot = &s;
        } else if (!spare || age >= spare_age) {
            spare = &s;
            spare_age = age;
        }
    }
    if (!slot) {
        slot = spare;
        drop(*slot);
        slot->buf = (uint8_t*)malloc((size_t)total * FRAG_DATA);
        if (!slot->buf) return 0;
        memcpy(slot->fid, frag + 1, 16);
        slot->total = total;
        slot->started_ms = now_ms;
    }
    if (slot->total != total) return 0;
    if (!(slot->got & (1u << seq))) {
        memcpy(slot->buf + (size_t)seq * FRAG_DATA, data, n);
        slot->len[seq] = (uint16_t)n;
        slot->got |= (uint16_t)(1u << seq);
    }
    if (slot->got != (uint16_t)((1u << total) - 1)) return 0;

    size_t whole = 0;
    for (uint16_t i = 0; i < total; i++) {
        memmove(slot->buf + whole, slot->buf + (size_t)i * FRAG_DATA, slot->len[i]);
        whole += slot->len[i];
    }
    *out = slot->buf;                         // the caller frees it
    slot->buf = nullptr;
    drop(*slot);
    return whole;
}

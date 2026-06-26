#include "serial_cli.h"
#include <Arduino.h>
#include <WiFi.h>
#include <vector>
#include "config.h"
#include "settings.h"
#include "wifi_setup.h"
#include "telesthete.h"
#include "ble_hid.h"
#include "serial_buf.h"  // CDCSerial + cliReadByte

static String g_buf;

// Output to USB CDC (the port the host sees as /dev/ttyACM*).
#define OUT CDCSerial

static void prompt() { OUT.print("rook> "); }

static std::vector<String> tokenize(const String& line) {
    std::vector<String> out;
    String cur;
    for (size_t i = 0; i < line.length(); i++) {
        char c = line[i];
        if (c == ' ' || c == '\t') {
            if (cur.length()) { out.push_back(cur); cur = ""; }
        } else {
            cur += c;
        }
    }
    if (cur.length()) out.push_back(cur);
    return out;
}

static void printHelp() {
    OUT.println(
        "commands:\n"
        "  help                            show this\n"
        "  status                          wifi + band + ble snapshot\n"
        "  ip                              show STA IP / AP info\n"
        "  wifi list                       saved networks\n"
        "  wifi add <ssid> <pass> [prio]   add or update (prio default 50)\n"
        "  wifi rm <ssid>                  remove network\n"
        "  wifi forget                     remove ALL saved networks\n"
        "  wifi scan                       scan visible APs\n"
        "  wifi reconnect                  force re-eval / reconnect\n"
        "  hub <host> [port]               set telesthete hub\n"
        "  band <psk>                      set band psk (reboot to apply)\n"
        "  worker <name>                   set worker name\n"
        "  admin <user> <pass>             set web admin creds\n"
        "  reboot                          restart\n"
        "  factory                         wipe NVS + reboot");
}

static void cmdStatus() {
    const auto& s = getSettings();
    OUT.printf("fw=%s worker=%s\n", ROOK_FW_VERSION, s.worker_name.c_str());
    if (WiFi.status() == WL_CONNECTED) {
        OUT.printf("wifi: connected ssid=%s ip=%s rssi=%d\n",
                   WiFi.SSID().c_str(),
                   WiFi.localIP().toString().c_str(),
                   WiFi.RSSI());
    } else {
        OUT.println("wifi: not connected");
    }
    OUT.printf("ap:   ssid=%s ip=%s\n",
               s.ap_ssid.c_str(),
               WiFi.softAPIP().toString().c_str());
    OUT.printf("hub:  %s:%u  worker_id=%s  hub_ok=%d  last_seen=%lus\n",
               s.hub_host.c_str(), s.hub_port,
               telestheteWorkerId().c_str(),
               (int)telestheteHubOk(),
               (unsigned long)telestheteLastSeenSec());
    OUT.printf("ble:  %s\n", bleHidConnected() ? "connected" : "advertising/idle");
}

static void cmdIp() {
    if (WiFi.status() == WL_CONNECTED)
        OUT.printf("sta %s  ssid=%s\n",
                   WiFi.localIP().toString().c_str(),
                   WiFi.SSID().c_str());
    else
        OUT.println("sta down");
    OUT.printf("ap  %s\n", WiFi.softAPIP().toString().c_str());
}

static void cmdWifiList() {
    auto nets = wifiListNetworks();
    if (nets.empty()) { OUT.println("(none)"); return; }
    for (const auto& n : nets) {
        OUT.printf("  p%-3d %-32s  (pass=%d chars)\n",
                   n.priority, n.ssid.c_str(), (int)n.pass.length());
    }
}

static void cmdWifiAdd(const std::vector<String>& tok) {
    if (tok.size() < 4) { OUT.println("usage: wifi add <ssid> <pass> [prio]"); return; }
    int prio = (tok.size() >= 5) ? tok[4].toInt() : 50;
    if (wifiAddOrUpdate(tok[2], tok[3], prio)) {
        OUT.printf("ok, '%s' saved at p%d. run 'wifi reconnect' to apply.\n",
                   tok[2].c_str(), prio);
    } else {
        OUT.println("failed");
    }
}

static void cmdWifiRm(const std::vector<String>& tok) {
    if (tok.size() < 3) { OUT.println("usage: wifi rm <ssid>"); return; }
    OUT.println(wifiRemove(tok[2]) ? "removed" : "not found");
}

static void cmdWifiScan() {
    OUT.println("scanning...");
    auto found = wifiScanVisible();
    if (found.empty()) OUT.println("(none)");
    else for (const auto& s : found) OUT.printf("  %s\n", s.c_str());
}

static void cmdHub(const std::vector<String>& tok) {
    if (tok.size() < 2) { OUT.println("usage: hub <host> [port]"); return; }
    DeviceSettings s = getSettings();
    s.hub_host = tok[1];
    if (tok.size() >= 3) s.hub_port = (uint16_t)tok[2].toInt();
    updateSettings(s);
    OUT.printf("hub set to %s:%u (reboot to reconnect)\n",
               s.hub_host.c_str(), s.hub_port);
}

static void cmdBand(const std::vector<String>& tok) {
    if (tok.size() < 2) { OUT.println("usage: band <psk>"); return; }
    DeviceSettings s = getSettings();
    s.band_psk = tok[1];
    updateSettings(s);
    OUT.println("band psk saved (reboot to apply)");
}

static void cmdWorker(const std::vector<String>& tok) {
    if (tok.size() < 2) { OUT.println("usage: worker <name>"); return; }
    DeviceSettings s = getSettings();
    s.worker_name = tok[1];
    updateSettings(s);
    OUT.printf("worker name = %s (reboot to re-announce)\n", s.worker_name.c_str());
}

static void cmdAdmin(const std::vector<String>& tok) {
    if (tok.size() < 3) { OUT.println("usage: admin <user> <pass>"); return; }
    DeviceSettings s = getSettings();
    s.admin_user = tok[1];
    s.admin_pass = tok[2];
    updateSettings(s);
    OUT.println("admin creds updated");
}

static void dispatch(const String& line) {
    auto tok = tokenize(line);
    if (tok.empty()) return;
    const String& c = tok[0];

    if      (c == "help" || c == "?")    printHelp();
    else if (c == "status")              cmdStatus();
    else if (c == "ip")                  cmdIp();
    else if (c == "wifi") {
        if (tok.size() < 2)                       OUT.println("wifi: list|add|rm|forget|scan|reconnect");
        else if (tok[1] == "list")                cmdWifiList();
        else if (tok[1] == "add")                 cmdWifiAdd(tok);
        else if (tok[1] == "rm" || tok[1] == "remove") cmdWifiRm(tok);
        else if (tok[1] == "forget")              { wifiForgetAll(); OUT.println("all forgotten"); }
        else if (tok[1] == "scan")                cmdWifiScan();
        else if (tok[1] == "reconnect")           { wifiKickReconnect(); OUT.println("kicked monitor"); }
        else                                       OUT.printf("unknown wifi subcommand: %s\n", tok[1].c_str());
    }
    else if (c == "hub")                 cmdHub(tok);
    else if (c == "band")                cmdBand(tok);
    else if (c == "worker")              cmdWorker(tok);
    else if (c == "admin")               cmdAdmin(tok);
    else if (c == "reboot")              { OUT.println("rebooting..."); delay(200); ESP.restart(); }
    else if (c == "factory")             { OUT.println("factory reset..."); factoryResetSettings(); delay(200); ESP.restart(); }
    else                                  OUT.printf("unknown: %s  (try 'help')\n", c.c_str());
}

static bool greeted = false;

void serialCliGreet() { /* greet on first connect — see pollSerialCli */ }

void pollSerialCli() {
    // Wait until host opens the CDC port before printing greeting.
    if (!greeted) {
        if (CDCSerial) {
            OUT.println();
            OUT.println("Rook KVM dongle CLI. Type 'help' for commands.");
            prompt();
            greeted = true;
        }
        return;
    }
    if (!CDCSerial) { greeted = false; g_buf = ""; return; }

    int c;
    while ((c = cliReadByte()) >= 0) {
        char ch = (char)c;
        if (ch == '\r') continue;
        if (ch == '\n') {
            OUT.println();
            dispatch(g_buf);
            g_buf = "";
            prompt();
        } else if (ch == 0x7f || ch == 0x08) {
            if (g_buf.length()) {
                g_buf.remove(g_buf.length() - 1);
                OUT.print("\b \b");
            }
        } else if (ch >= 0x20 && ch < 0x7f) {
            g_buf += ch;
            OUT.print(ch);
        }
    }
}

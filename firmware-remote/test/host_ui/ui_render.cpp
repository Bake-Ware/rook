// Renders ui.cpp's screens on a PC to PBM files (test/host_ui), to look at the
// layouts: the status screen with a profile's pictures and the last key's box
// (fresh and plain), "nothing for", no hub, the Devices tab, a profile's
// options. Built twice: -DROOK_BRIDGE (128x64) and without (the handheld's 32x128):
//   cd firmware-remote/test/host_ui && G="../../.pio/libdeps/bridge/Adafruit GFX Library" &&
//   g++ -std=gnu++17 -w -DARDUINO=100 [-DROOK_BRIDGE=1] -include Print.h -I. -I../host -I../../src -I"$G" \
//     ui_render.cpp ../../src/ui.cpp "$G/Adafruit_GFX.cpp" -o /tmp/render && /tmp/render <dir for the .pbm files>
#include <stdio.h>
#include <string>
#include <vector>
#include "Adafruit_SSD1306.h"
#include "ui.h"
TwoWire Wire;
HostEsp ESP;
namespace tetris { void draw(Adafruit_GFX&) {} }
namespace ui { bool snapshot(std::string& out, int& w, int& h, int& rot, bool& dark); }

static void dump(const char* name) {
    std::string px;
    int w, h, rot;
    bool dark;
    ui::snapshot(px, w, h, rot, dark);
    // the panel's buffer: GFXcanvas1 rows of (w+7)/8 bytes, top bit left; turned by rot
    int W = rot & 1 ? h : w, H = rot & 1 ? w : h;
    FILE* f = fopen(name, "w");
    fprintf(f, "P1\n%d %d\n", W, H);
    for (int y = 0; y < H; y++) {
        for (int x = 0; x < W; x++) {
            int px_x = x, px_y = y;
            if (rot == 1) { px_x = w - 1 - y; px_y = x; }        // as GFXcanvas1 turns a pixel
            else if (rot == 3) { px_x = y; px_y = h - 1 - x; }
            uint8_t b = px[px_y * ((w + 7) / 8) + px_x / 8];
            fputc(b >> (7 - px_x % 8) & 1 ? '1' : '0', f);
        }
        fputc('\n', f);
    }
    fclose(f);
}

int main(int argc, char** argv) {
    std::string dir = argc > 1 ? argv[1] : ".";
#ifdef ROOK_BRIDGE
    const char* tag = "bridge";
    ui::begin(0x3C, 0);
#else
    const char* tag = "handheld";
    ui::begin(0x3C, 1);
#endif
    auto shot = [&](const char* what) {
        ui::tick();
        dump((dir + "/" + tag + "-" + what + ".pbm").c_str());
    };
    ui::status_line(0, "band");
    ui::status_line(1, "TV: B");
    ui::network("HomeNet");
    ui::status_line(2, "LG TVs");
    ui::status_icon(1);                              // tv
    ui::status_routes(ui::ROUTE_IR | ui::ROUTE_ROOK);
    ui::key_note(ui::NOTE_IR, "vol_up", 1000);
    shot("ir-fresh");
    ui::key_note(ui::NOTE_IR, "vol_up", 0);
    shot("ir-plain");
    ui::key_note(ui::NOTE_ROOK, "bt_c77", 0);
    ui::status_routes(ui::ROUTE_IR | ui::ROUTE_ROOK | ui::ROUTE_WIFI);
    shot("rook-3routes");
    ui::status_line(0, "hub?");
    ui::key_note(ui::NOTE_NO_HUB, "play", 0);
    shot("nohub");
    ui::key_note(ui::NOTE_NONE, "nothing for bt_c19c", 0);
    shot("nothing");
    ui::status_line(2, "Steam Deck");
    ui::status_icon(8);
    ui::status_routes(ui::ROUTE_ROOK);
    ui::key_note(ui::NOTE_ROOK, "ok", 0);
    shot("steamdeck");
    std::vector<std::string> names = {"Devices", "Assistant", "Games", "Settings"};
    ui::menu_tabs(names, 0, {"LG TVs", "Steam Deck", "Soundbar", "Add device"}, 0, nullptr);
    ui::menu_icons_set({1, 8, 3, ui::ICON_PLUS});
    ui::menu_routes_set({ui::ROUTE_IR | ui::ROUTE_ROOK, ui::ROUTE_ROOK, ui::ROUTE_IR | ui::ROUTE_WIFI, 0});
    shot("devices");
    ui::menu_end();
    ui::menu_begin("LG TVs", {"Rename", "Icon", "Delete"}, 0);
    ui::menu_title_icons(1, ui::ROUTE_IR | ui::ROUTE_ROOK);
    shot("options");
    ui::menu_end();
    return 0;
}

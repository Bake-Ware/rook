// Firmware updates over the air: the remote pulls a build from a URL into its
// spare app slot, checks it against the SHA-256 it was given (that hash came
// over the band, or from the feed below, so the download itself may be plain
// http or unverified https), and restarts into it. A download that stalls or
// breaks off is tried again a few times, from where it got to (an HTTP Range
// request; a server that ignores it is read past what was had). While it runs,
// quiet(true) asks the rest of the remote to keep the radio free (no Bluetooth
// scanning). The new build is on trial: it is kept once it has settled (on
// Wi-Fi, and on a bridge with a bonded remote, that remote back) or reached
// the hub, whichever comes first; five minutes with neither and the bootloader
// goes back to the build before.
// check() is the direct route, no hub needed: it reads a small feed,
// FEED_BASE + latest-<board>.json, {version, moniker, url, sha256}, and installs
// the build when it is newer than this one. When the feed cannot be had, the
// caller is told (take_fallback) and asks the hub instead.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <functional>
#include <string>

namespace ota {

static const char* const FEED_BASE = "http://rook.bakeforge.com/firmware/";

void begin(std::function<void(const char*)> log, std::function<void(const std::string&)> show,
           std::function<void(bool)> quiet = nullptr);
// Call often: keeps a build on trial once it has settled or the hub is reached, or gives it up.
void tick(bool hub_ok, bool settled);
void confirm();                        // keep the build on trial (by hand, over serial)
bool busy();
// Start an update in the background. False with err when it can't start.
bool start(const std::string& url, const std::string& sha256, std::string& err);
// Read the feed for board (bridge-c3, s3...) in the background and install
// what it offers when it is newer than version. False with err when it can't start.
bool check(const std::string& board, const std::string& version, std::string& err);
// The feed could not be read since the last call: why. False: nothing to report.
bool take_fallback(std::string& why);
// state (idle, checking, newest, downloading, failed, restarting), percent,
// error, slot, trial; after a failed download: tries, heap_min, and free heap
// and largest block when it failed.
void fill(JsonObject o);

}  // namespace ota

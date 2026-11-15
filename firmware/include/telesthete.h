#pragma once
#include <Arduino.h>

// Bring up the Telesthete band worker: derives band_id+key from PSK, opens
// a UDP socket to the configured relay, registers (sends one keepalive frame),
// then speaks the minimal worker profile: announce, heartbeat, answer calls.
void setupTelesthete();

// True if we've received any frame from the hub recently.
bool telestheteHubOk();

// Seconds since the last frame from any peer on the band (0 if never).
uint32_t telestheteLastSeenSec();

// Worker id (32 hex from the chip's MAC: the same after every restart).
const String& telestheteWorkerId();

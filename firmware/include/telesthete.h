#pragma once
#include <Arduino.h>

// Bring up the Telesthete band worker: derives band_id+key from PSK, opens
// a UDP socket to the configured hub, registers (sends one keepalive frame),
// then announces capabilities and answers RPC calls.
void setupTelesthete();

// True if we've received any frame from the hub recently.
bool telestheteHubOk();

// Seconds since the last frame from any peer on the band (0 if never).
uint32_t telestheteLastSeenSec();

// Worker id (random hex, generated on first call).
const String& telestheteWorkerId();

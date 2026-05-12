#pragma once
#include <Arduino.h>

// Polls BUTTON_PIN (active-low, internal pull-up). Short press triggers
// the storage-mode toggle (reboot into the other mode). Long holds are
// ignored to prevent repeat fires.
//
// Call setupButton() once in setup(); call pollButton() from loop().
void setupButton();
void pollButton();

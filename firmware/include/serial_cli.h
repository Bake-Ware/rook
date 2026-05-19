#pragma once

// Read a line from Serial (USB-Serial-JTAG) and dispatch. Call from loop().
void pollSerialCli();

// Print the prompt + greeting once at startup.
void serialCliGreet();

#pragma once
#include <Arduino.h>
#include "USBCDC.h"
#include "config.h"

extern USBCDC CDCSerial;
extern char serialBuf[SERIAL_BUF_SIZE];
extern volatile size_t serialBufLen;
extern portMUX_TYPE bufMux;

// File transfer state
extern volatile bool fileTransferActive;
extern char fileTransferName[65];
extern volatile size_t fileTransferBytes;

void pollCDC();

// CLI input ring fed by pollCDC. Returns next byte or -1 if empty.
int cliReadByte();

#include "serial_buf.h"
#include "storage.h"
#include "ws.h"
#include "SD_MMC.h"
#include "mbedtls/base64.h"
#include <string.h>

USBCDC CDCSerial(0);
char serialBuf[SERIAL_BUF_SIZE];
volatile size_t serialBufLen = 0;
portMUX_TYPE bufMux = portMUX_INITIALIZER_UNLOCKED;

// ---- File Transfer State Machine ----
enum SerialState { SERIAL_NORMAL, SERIAL_FILE_HEADER, SERIAL_FILE_DATA };
static SerialState state = SERIAL_NORMAL;

// Marker detection
static const char* MARKER_BEGIN = FILE_MARKER_BEGIN;  // "<<<ROOK_FILE:"
static const char* MARKER_EOF   = FILE_MARKER_END;    // "<<<ROOK_EOF>>>"
static const int MARKER_BEGIN_LEN = 13;
static const int MARKER_EOF_LEN = 14;

// Match buffer for detecting markers across chunk boundaries
static char matchBuf[16];
static int matchLen = 0;

// File header accumulator
static char headerBuf[128];
static int headerLen = 0;

// File state
volatile bool fileTransferActive = false;
char fileTransferName[65] = {0};
volatile size_t fileTransferBytes = 0;
static File currentFile;

// Base64 decode buffer — decode in chunks
#define B64_BUF_SIZE 4096
static uint8_t b64Buf[B64_BUF_SIZE];
static size_t b64Len = 0;
static unsigned long lastFileDataTime = 0;

static void flushB64() {
    if (b64Len == 0 || !currentFile) return;
    // Align to 4-byte boundary
    size_t aligned = (b64Len / 4) * 4;
    if (aligned == 0) return;

    size_t outLen = 0;
    uint8_t decoded[3 * (aligned / 4)];
    int ret = mbedtls_base64_decode(decoded, sizeof(decoded), &outLen, b64Buf, aligned);
    if (ret == 0 && outLen > 0) {
        currentFile.write(decoded, outLen);
        fileTransferBytes += outLen;
    }

    // Shift remainder
    size_t rem = b64Len - aligned;
    if (rem > 0) memmove(b64Buf, b64Buf + aligned, rem);
    b64Len = rem;
}

static void startFileTransfer(const char* filename) {
    strncpy(fileTransferName, filename, 64);
    fileTransferName[64] = '\0';

    String path = String("/drop/") + fileTransferName;
    currentFile = SD_MMC.open(path, FILE_WRITE);
    fileTransferActive = true;
    fileTransferBytes = 0;
    b64Len = 0;
    lastFileDataTime = millis();
}

static void endFileTransfer(bool success) {
    flushB64();  // flush remaining
    size_t finalSize = fileTransferBytes;
    if (currentFile) {
        currentFile.close();
    }
    if (success && finalSize > 0) {
        String path = String("drop/") + fileTransferName;
        wsSendFileReady(path.c_str(), finalSize);
    } else if (!success) {
        // Clean up partial file
        String path = String("/drop/") + fileTransferName;
        SD_MMC.remove(path);
    }
    fileTransferActive = false;
    fileTransferName[0] = '\0';
    fileTransferBytes = 0;
    b64Len = 0;
    state = SERIAL_NORMAL;
    matchLen = 0;
}

// Parallel ring buffer for the serial CLI. processByte() pushes every
// SERIAL_NORMAL byte here in addition to the WS-stream buffer so the TUI
// can read keystrokes while websocket clients keep tunneling.
#define CLI_RING_SIZE 256
static volatile uint8_t cliRing[CLI_RING_SIZE];
static volatile uint16_t cliHead = 0, cliTail = 0;
static portMUX_TYPE cliMux = portMUX_INITIALIZER_UNLOCKED;

static void cliPush(char c) {
    taskENTER_CRITICAL(&cliMux);
    uint16_t next = (cliHead + 1) % CLI_RING_SIZE;
    if (next != cliTail) {
        cliRing[cliHead] = (uint8_t)c;
        cliHead = next;
    }  // drop on full
    taskEXIT_CRITICAL(&cliMux);
}

int cliReadByte() {
    int out = -1;
    taskENTER_CRITICAL(&cliMux);
    if (cliTail != cliHead) {
        out = cliRing[cliTail];
        cliTail = (cliTail + 1) % CLI_RING_SIZE;
    }
    taskEXIT_CRITICAL(&cliMux);
    return out;
}

static void addToSerialBuf(char c) {
    cliPush(c);
    taskENTER_CRITICAL(&bufMux);
    if (serialBufLen < SERIAL_BUF_SIZE - 1)
        serialBuf[serialBufLen++] = c;
    taskEXIT_CRITICAL(&bufMux);
}

// Feed one byte at a time through the state machine
static void processByte(char c) {
    switch (state) {
    case SERIAL_NORMAL:
        // Try to match FILE_MARKER_BEGIN
        if (c == MARKER_BEGIN[matchLen]) {
            matchBuf[matchLen++] = c;
            if (matchLen == MARKER_BEGIN_LEN) {
                // Full match — switch to header mode
                state = SERIAL_FILE_HEADER;
                headerLen = 0;
                matchLen = 0;
            }
        } else {
            // Not a match — flush accumulated match chars to serial buf
            for (int i = 0; i < matchLen; i++)
                addToSerialBuf(matchBuf[i]);
            matchLen = 0;
            // Check if this byte starts a new match
            if (c == MARKER_BEGIN[0]) {
                matchBuf[matchLen++] = c;
            } else {
                addToSerialBuf(c);
            }
        }
        break;

    case SERIAL_FILE_HEADER:
        // Accumulate until we see ">>>"
        if (headerLen < 127) {
            headerBuf[headerLen++] = c;
            headerBuf[headerLen] = '\0';
            // Check for ">>>" at end of header
            if (headerLen >= 3 && strcmp(&headerBuf[headerLen - 3], ">>>") == 0) {
                headerBuf[headerLen - 3] = '\0';  // strip ">>>"
                // Parse: "filename;base64"
                char* semi = strchr(headerBuf, ';');
                if (semi) *semi = '\0';
                // headerBuf is now the filename
                if (isStorageReady() && strlen(headerBuf) > 0) {
                    startFileTransfer(headerBuf);
                    state = SERIAL_FILE_DATA;
                } else {
                    state = SERIAL_NORMAL;
                }
                matchLen = 0;
            }
        } else {
            // Header too long — abort
            state = SERIAL_NORMAL;
            matchLen = 0;
        }
        break;

    case SERIAL_FILE_DATA:
        lastFileDataTime = millis();
        // Check for EOF marker
        if (c == MARKER_EOF[matchLen]) {
            matchBuf[matchLen++] = c;
            if (matchLen == MARKER_EOF_LEN) {
                endFileTransfer(true);
            }
        } else {
            // Not EOF marker — flush match chars as base64 data
            for (int i = 0; i < matchLen; i++) {
                char mc = matchBuf[i];
                if (mc != '\r' && mc != '\n' && mc != ' ') {
                    if (b64Len < B64_BUF_SIZE)
                        b64Buf[b64Len++] = (uint8_t)mc;
                    if (b64Len >= B64_BUF_SIZE)
                        flushB64();
                }
            }
            matchLen = 0;
            // Process current byte
            if (c == MARKER_EOF[0]) {
                matchBuf[matchLen++] = c;
            } else if (c != '\r' && c != '\n' && c != ' ') {
                if (b64Len < B64_BUF_SIZE)
                    b64Buf[b64Len++] = (uint8_t)c;
                if (b64Len >= B64_BUF_SIZE)
                    flushB64();
            }
        }
        break;
    }
}

void pollCDC() {
    // Timeout check for stale file transfers (30s no data)
    if (state == SERIAL_FILE_DATA && (millis() - lastFileDataTime > 30000)) {
        endFileTransfer(false);
    }

    while (CDCSerial.available()) {
        int c = CDCSerial.read();
        if (c >= 0) {
            processByte((char)c);
        }
    }
}

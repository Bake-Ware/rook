# Rook KVM firmware — build / OTA / flash / monitor helpers.
#
#   make build                      compile
#   make ota HOST=192.168.1.50      OTA flash over WiFi (HTTP basic-auth)
#   make flash                      USB flash (hold the BOOT button)
#   make monitor                    serial monitor
#   make clean
#
# Overridable vars:
#   PIO          path to platformio        (default: pio on PATH)
#   HOST         dongle IP for OTA
#   ADMIN_USER   HTTP basic-auth user      (default: bake)
#   ADMIN_PASS   HTTP basic-auth pass      (default: poop)
#
# secrets.h note: WiFi creds + band PSK live in the dongle's NVS and survive
# OTA, so a build with a placeholder secrets.h is safe to OTA onto a dongle
# that's already provisioned. Only a fresh/factory-reset device needs real
# values compiled in (or set them afterward via /config).

PIO        ?= pio
HOST       ?=
ADMIN_USER ?= bake
ADMIN_PASS ?= poop
BIN        := .pio/build/t-dongle-s3/firmware.bin

.PHONY: build flash ota monitor clean check-host

build:
	$(PIO) run

flash:
	$(PIO) run -t upload

monitor:
	$(PIO) device monitor

clean:
	$(PIO) run -t clean

check-host:
	@test -n "$(HOST)" || { echo "ERROR: set HOST=<dongle-ip>  (e.g. make ota HOST=192.168.1.50)"; exit 1; }

ota: build check-host
	@echo "OTA -> http://$(HOST)/ota"
	curl -fsS -u "$(ADMIN_USER):$(ADMIN_PASS)" -F "firmware=@$(BIN)" "http://$(HOST)/ota"; echo

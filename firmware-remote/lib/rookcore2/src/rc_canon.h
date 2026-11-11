// RFC 8785 canonical JSON over ArduinoJson values, matching rook/core2/wire.py
// (canonical, args_hash, signing_input). Spec core-v2 3.2.
#pragma once
#include <ArduinoJson.h>
#include <string>

namespace rc {

// Canonical form of v. Returns false when v has no canonical form (an integer
// outside +-(2^53-1), NaN/Infinity).
bool canonical(JsonVariantConst v, std::string& out);

// base64url(SHA-256(canonical(args))), unpadded; null/missing args hash as {}.
bool args_hash(JsonVariantConst args, std::string& out);

// PREFIX[typ] + canonical(obj minus "sig" and "hubkey"). False for an unknown
// typ or a value without a canonical form.
bool signing_input(JsonObjectConst obj, std::string& out);

}  // namespace rc

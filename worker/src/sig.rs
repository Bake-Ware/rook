//! Signed objects (core v2 10.1), verify only: Ed25519 over a type prefix
//! and the canonical JSON of the object without `sig` and `hubkey`. A
//! release is checked as anchor.json's root key -> the `rook-hubkey` grant
//! it signed -> the operational key -> `release.sig`.

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine;
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::manifests::canonical;

fn prefix(typ: &str) -> Option<&'static str> {
    Some(match typ {
        "rook-ticket" => "rook-ticket-v2\n",
        "rook-lease" => "rook-lease-v2\n",
        "rook-hubkey" => "rook-hubkey-v2\n",
        "rook-membership" => "rook-membership-v2\n",
        "rook-announce" => "rook-announce-v2\n",
        "rook-plugin" => "rook-plugin-v2\n",
        "rook-release" => "rook-release-v2\n",
        "rook-request" => "rook-request-v2\n",
        _ => return None,
    })
}

fn raw_pub(pubkey: &str) -> Option<[u8; 32]> {
    B64.decode(pubkey.strip_prefix("ed25519:").unwrap_or(pubkey)).ok()?.try_into().ok()
}

/// The first 16 hex of SHA-256 over the raw public key.
pub fn key_id(pubkey: &str) -> Option<String> {
    Some(crate::link::hex(&Sha256::digest(raw_pub(pubkey)?))[..16].to_string())
}

pub fn signing_input(obj: &Value) -> Option<String> {
    let mut body = obj.as_object()?.clone();
    body.remove("sig");
    body.remove("hubkey");
    Some(format!("{}{}", prefix(obj["typ"].as_str()?)?, canonical(&Value::Object(body))))
}

/// Whether `obj["sig"]` verifies under `pubkey` ("ed25519:<base64>").
pub fn verify(pubkey: &str, obj: &Value) -> bool {
    let check = || -> Option<()> {
        let key = VerifyingKey::from_bytes(&raw_pub(pubkey)?).ok()?;
        let sig: [u8; 64] = B64.decode(obj["sig"].as_str()?).ok()?.try_into().ok()?;
        key.verify_strict(signing_input(obj)?.as_bytes(), &Signature::from_bytes(&sig)).ok()
    };
    check().is_some()
}

/// The operational key a `rook-hubkey` grant certifies, when the root signed it.
pub fn grant_key(grant: &Value, root: &str) -> Option<String> {
    (grant["typ"] == "rook-hubkey" && verify(root, grant)).then(|| grant["key"].as_str().map(String::from))?
}

#[cfg(test)]
mod tests {
    use super::*;

    fn vectors() -> Value {
        serde_json::from_str(include_str!("../../conformance/core-v2/vectors/signatures.json")).unwrap()
    }

    #[test]
    fn key_ids_and_signing_input_match() {
        let v = vectors();
        for k in v["keys"].as_array().unwrap() {
            assert_eq!(key_id(k["pub"].as_str().unwrap()).unwrap(), k["kid"].as_str().unwrap());
        }
        let si = &v["signing_input"];
        assert_eq!(signing_input(&si["object"]).unwrap(), si["bytes_utf8"].as_str().unwrap());
    }

    #[test]
    fn signatures_verify_as_the_vectors_say() {
        let v = vectors();
        for case in v["verify"].as_array().unwrap() {
            let got = verify(case["pub"].as_str().unwrap(), &case["object"]);
            assert_eq!(got, case["valid"].as_bool().unwrap(), "{}", case["name"]);
        }
        let ok = v["hubkey"].as_array().unwrap().iter().find(|c| c["name"] == "ok").unwrap();
        assert_eq!(grant_key(&ok["grant"], ok["root"].as_str().unwrap()).as_deref(), ok["key"].as_str());
    }
}

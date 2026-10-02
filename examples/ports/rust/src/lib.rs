//! A minimal Rook worker in Rust, written from docs/spec/core-v1.md.
//!
//! * [`band`]: the telesthete subset (band id/key, CHANNEL frames,
//!   fragmentation, UDP link to the relay)
//! * [`canonical`]: canonical JSON and the ticket args hash
//! * [`authz`]: tiers, grants, signed announces, tickets
//! * [`placement`]: placement expressions over node facts
//! * [`registry`]: caps with declared params and the limit/fields contract
//! * [`worker`]: dispatch rules, announces, and calls to other nodes

pub mod authz;
pub mod band;
pub mod canonical;
pub mod placement;
pub mod registry;
pub mod worker;

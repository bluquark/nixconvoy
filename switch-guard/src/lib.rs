//! nixconvoy-confirmed-switch (nix/kit.nix), nixconvoy-boot-assessment
//! (modules/boot-assessment.nix), nixconvoy's side of the switch protocol
//! (control; the helper is the target's) and its git and drift decisions
//! (plan) as Sans-I/O state machines: the pure cores decide, the drivers
//! perform their effects.
pub mod boot;
pub mod config;
pub mod control;
pub mod plan;
pub mod proc;
pub mod record;
pub mod strings;
pub mod switch;
pub mod wire;

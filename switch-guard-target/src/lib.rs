//! The target's side of switch-guard: nixconvoy-confirmed-switch
//! (nix/kit.nix), the driver performing the effects switch-guard's switch
//! core decides.  Linux alone (/proc, systemd).
pub mod config;
pub mod proc;
pub mod switch;

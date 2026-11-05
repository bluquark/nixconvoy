//! The target's side of switch-guard: nixconvoy-confirmed-switch
//! (nix/kit.nix) and nixconvoy-boot-assessment
//! (modules/boot-assessment.nix), the drivers performing the effects
//! switch-guard's cores decide.  Linux alone (/proc, systemd).
pub mod boot;
pub mod config;
pub mod proc;
pub mod switch;

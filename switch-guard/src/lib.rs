//! confirmed-switch (modules/confirmed-switch.nix), boot-assessment
//! (modules/boot-assessment.nix) and nixconvoy's switch (deployer) and git
//! and drift decisions (plan) as Sans-I/O state machines: the pure cores
//! decide, the drivers perform their effects.
pub mod boot;
pub mod config;
pub mod deployer;
pub mod plan;
pub mod proc;
pub mod record;
pub mod switch;

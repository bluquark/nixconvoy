//! nixconvoy's side of the switch protocol (control; the helper is the
//! target's: switch), boot assessment's decisions (boot) and nixconvoy's git
//! and drift decisions (plan) as Sans-I/O state machines: the pure cores
//! decide.  nixconvoy-switch and nixconvoy-plan serve nixconvoy's; the
//! target's drivers, which perform the effects, are switch-guard-target/.
pub mod boot;
pub mod control;
pub mod plan;
pub mod record;
pub mod strings;
pub mod switch;
pub mod wire;

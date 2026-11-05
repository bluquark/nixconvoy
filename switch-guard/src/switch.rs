//! confirmed-switch: activate under a dead-man timer, confirm or roll back.
pub mod core;
#[cfg(test)]
pub(crate) mod fake;
#[cfg(test)]
mod model;

//! confirmed-switch: activate under a dead-man timer, confirm or roll back.
pub mod core;
pub mod driver;
#[cfg(test)]
mod model;

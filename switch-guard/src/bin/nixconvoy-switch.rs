//! nixconvoy's switch step, one target per process: see control/wire.rs.
use switch_guard::control::{wire, Machine, Reply};

fn main() {
    switch_guard::wire::serve(
        "nixconvoy-switch",
        |line| wire::params(line).map(Machine::new),
        |m: &mut Machine, reply| wire::step(&m.decide(reply.unwrap_or(Reply::Start))),
        wire::reply,
    );
}

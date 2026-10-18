//! nixconvoy's checkout and drift decisions, one core per process: see
//! plan/wire.rs.
use switch_guard::plan::wire;

fn main() {
    switch_guard::wire::serve("nixconvoy-plan", wire::start, |core, reply| core.decide(reply), wire::reply);
}

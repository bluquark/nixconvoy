fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    std::process::exit(switch_guard::switch::driver::main(&args));
}

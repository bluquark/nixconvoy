# The crate behind nixconvoy's nixconvoy-switch and nixconvoy-plan
# (switch-guard/: the cores, see its src/lib.rs; the target's drivers on
# them are switch-guard-target.nix's).  checkPhase's `cargo test` runs the
# stateright models and the property tests.
{ pkgs }:
let
  inherit (pkgs) lib stdenv;
  fs = lib.fileset;
  fileset = fs.unions [
    ../switch-guard/Cargo.toml
    ../switch-guard/Cargo.lock
    ../switch-guard/src
  ];
  drv = pkgs.rustPlatform.buildRustPackage {
  pname = "switch-guard";
  version = "0.1.0";
  src = fs.toSource {
    root = ../switch-guard;
    inherit fileset;
  };
  # unchecked: the binaries alone, for the test suite's source-tree runs.
  passthru = { inherit fileset; unchecked = drv.overrideAttrs { doCheck = false; }; };
  cargoLock.lockFile = ../switch-guard/Cargo.lock;
  # Bounded so a runaway model or allocation fails this build instead of
  # exhausting the builder: every later phase and child inherits RLIMIT_AS
  # of 4 GiB per process (the composed model peaks around 1.4 GiB
  # resident), with at most two compile jobs and one test thread.  Linux
  # only: darwin's RLIMIT_AS is not enforced, and setting it may fail.
  preBuild = ''
    ${lib.optionalString stdenv.hostPlatform.isLinux "ulimit -v ${toString (4 * 1024 * 1024)}"}
    export NIX_BUILD_CORES=$(( NIX_BUILD_CORES < 2 ? NIX_BUILD_CORES : 2 ))
  '';
  # The cores have no platform code: Linux's run of the tests stands for darwin's.
  doCheck = stdenv.hostPlatform.isLinux;
  dontUseCargoParallelTests = true;
  # Cargo.toml's `checked` profile: overflow fails a test.
  checkType = "checked";
  };
in
drv

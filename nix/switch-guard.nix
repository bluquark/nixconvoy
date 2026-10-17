# The crate behind nixconvoy-confirmed-switch (the helper), nixconvoy-boot-assessment and
# nixconvoy's nixconvoy-switch and nixconvoy-plan (switch-guard/, one crate: see
# its src/lib.rs).  checkPhase's `cargo test` runs the stateright models and
# the property tests.
{ pkgs }:
let
  inherit (pkgs) lib stdenv;
  fs = lib.fileset;
in
pkgs.rustPlatform.buildRustPackage {
  pname = "switch-guard";
  version = "0.1.0";
  src = fs.toSource {
    root = ../switch-guard;
    fileset = fs.unions [
      ../switch-guard/Cargo.toml
      ../switch-guard/Cargo.lock
      ../switch-guard/src
    ];
  };
  cargoLock.lockFile = ../switch-guard/Cargo.lock;
  # Bounded so a runaway model or allocation fails this build instead of
  # exhausting the builder: every later phase and child inherits RLIMIT_AS
  # of 3 GiB per process (the suite passes under 1 GiB), with at most two
  # compile jobs and one test thread.  Linux only: darwin's RLIMIT_AS is not
  # enforced, and setting it may fail.
  preBuild = ''
    ${lib.optionalString stdenv.hostPlatform.isLinux "ulimit -v ${toString (3 * 1024 * 1024)}"}
    export NIX_BUILD_CORES=$(( NIX_BUILD_CORES < 2 ? NIX_BUILD_CORES : 2 ))
  '';
  dontUseCargoParallelTests = true;
  # Cargo.toml's `checked` profile: overflow fails a test.
  checkType = "checked";
  # The source this build is of, for tests/switch_test.py to refuse a build
  # of another tree (sha256 over `sha256sum` of every source file, sorted).
  postInstall = ''
    (cd $src && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1) > $out/source-hash
  '';
  meta.mainProgram = "nixconvoy-confirmed-switch";
}

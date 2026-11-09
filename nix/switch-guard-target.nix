# The crate behind nixconvoy-confirmed-switch (the helper:
# switch-guard-target/, the driver on switch-guard/'s core by path).
# Linux alone.
{ pkgs }:
let
  fs = pkgs.lib.fileset;
  fileset = fs.unions [
    ../switch-guard-target/Cargo.toml
    ../switch-guard-target/Cargo.lock
    ../switch-guard-target/src
    ../switch-guard/Cargo.toml
    ../switch-guard/src
  ];
in
pkgs.rustPlatform.buildRustPackage {
  pname = "switch-guard-target";
  version = "0.1.0";
  src = fs.toSource {
    root = ../.;
    inherit fileset;
  };
  sourceRoot = "source/switch-guard-target";
  passthru = { inherit fileset; };
  cargoLock.lockFile = ../switch-guard-target/Cargo.lock;
  # Cargo.toml's `checked` profile: overflow fails a test.
  checkType = "checked";
  meta.mainProgram = "nixconvoy-confirmed-switch";
}

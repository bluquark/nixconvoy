# The repo's own checks: ruff and pyrefly over the Python, rustfmt and
# clippy over the crate (cargo test runs in the crate build itself), and
# the switch test driving the real nixconvoy-switch against a fake target.
{ pkgs, switch-guard, nixconvoy }:
let
  inherit (pkgs) lib;
  fs = lib.fileset;
  pySources = fs.toSource {
    root = ../.;
    fileset = fs.unions [ ../src ../tests ../ruff.toml ../pyrefly.toml ../pyproject.toml ];
  };
  py = pkgs.runCommandLocal "nixconvoy-lint-py" { } ''
    cd ${pySources}
    HOME=$TMPDIR ${pkgs.ruff}/bin/ruff check --no-cache .
    HOME=$TMPDIR ${pkgs.pyrefly}/bin/pyrefly check --config pyrefly.toml --output-format min-text \
      --python-interpreter-path ${pkgs.python3}/bin/python3
    touch $out
  '';
  # Not runCommandLocal: a real test, which keeps the sandbox wherever it
  # is built.
  switchTest = pkgs.runCommand "nixconvoy-switch-test" { } ''
    cd ${pySources}
    PATH=${lib.makeBinPath [ pkgs.util-linux pkgs.git pkgs.mergiraf ]}:$PATH HOME=$TMPDIR \
      NIXCONVOY_GUARD_DIR=${switch-guard}/bin NIXCONVOY_GUARD_SRC=${switch-guard.src} PYTHONPATH=src \
      ${pkgs.python3}/bin/python3 tests/switch_test.py
    touch $out
  '';
  rustLint = pkgs.rustPlatform.buildRustPackage {
    pname = "nixconvoy-lint-rust";
    inherit (switch-guard) version cargoDeps preBuild;
    src = fs.toSource {
      root = ../switch-guard;
      fileset = fs.union switch-guard.fileset ../switch-guard/rustfmt.toml;
    };
    nativeBuildInputs = [ pkgs.rustfmt pkgs.clippy ];
    buildPhase = ''
      runHook preBuild
      cargo fmt --check
      cargo clippy --all-targets --offline -j "$NIX_BUILD_CORES" -- -D warnings
      runHook postBuild
    '';
    doCheck = false;
    installPhase = "touch $out";
  };
in
pkgs.runCommandLocal "nixconvoy-lint" { parts = [ py switchTest rustLint nixconvoy ]; } ''
  touch $out
''

# The repo's own checks: ruff and pyrefly over the Python, shellcheck over
# the plugins' hook scripts, rustfmt and clippy over the crate (cargo test runs
# in the crate build itself), and the switch test driving the real
# nixconvoy-switch against a fake target.
{ pkgs, switch-guard, nixconvoy }:
let
  inherit (pkgs) lib;
  fs = lib.fileset;
  # The plugin's hook script rides along for the switch test, which runs it.
  pySources = fs.toSource {
    root = ../.;
    fileset = fs.unions [ ../src ../tests ../integrations/claude-code/hooks ../ruff.toml ../pyrefly.toml ../pyproject.toml ];
  };
  hookScripts = [ "claude-code/hooks/run.sh" "claude-code-isolate/hooks/subagent-isolate.sh" ];
  hookSources = fs.toSource { root = ../integrations; fileset = fs.unions (map (f: ../integrations + "/${f}") hookScripts); };
  sh = pkgs.runCommandLocal "nixconvoy-lint-sh" { } ''
    ${pkgs.shellcheck-minimal}/bin/shellcheck ${lib.concatMapStringsSep " " (f: "${hookSources}/${f}") hookScripts}
    touch $out
  '';
  py = pkgs.runCommandLocal "nixconvoy-lint-py" { } ''
    cd ${pySources}
    HOME=$TMPDIR ${pkgs.ruff}/bin/ruff check --no-cache .
    HOME=$TMPDIR ${pkgs.pyrefly}/bin/pyrefly check --config pyrefly.toml --output-format min-text \
      --python-interpreter-path ${pkgs.python3}/bin/python3
    touch $out
  '';
  # Against the built package, the cores it carries included.  Not
  # runCommandLocal: a real test, which keeps the sandbox wherever it is
  # built.
  switchTest = pkgs.runCommand "nixconvoy-switch-test" { } ''
    cd ${pySources}
    PATH=${lib.makeBinPath [ pkgs.util-linux pkgs.git pkgs.mergiraf pkgs.jq ]}:$PATH HOME=$TMPDIR \
      PYTHONPATH=${nixconvoy}/${pkgs.python3.sitePackages} \
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
pkgs.runCommandLocal "nixconvoy-lint" { parts = [ py sh switchTest rustLint nixconvoy ]; } ''
  touch $out
''

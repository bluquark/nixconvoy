# The repo's static checks, cheap enough to gate every deploy that moves
# the pin: ruff and pyrefly over the Python, shellcheck over the plugins'
# hook scripts, rustfmt and clippy over the crates (the target's on Linux
# alone).  The tests: cargo test in each crate's build, the suite in
# nix/tests.nix.
{ pkgs, switch-guard, switch-guard-target ? null }:
let
  inherit (pkgs) lib;
  fs = lib.fileset;
  pySources = fs.toSource {
    root = ../.;
    fileset = fs.unions [ ../src ../tests ../ruff.toml ../pyrefly.toml ../pyproject.toml ];
  };
  hookScripts = [ "claude-code/hooks/run.sh" "claude-code/hooks/subagent-isolate.sh" ];
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
  rustLint = dir: crate: pkgs.rustPlatform.buildRustPackage {
    pname = "nixconvoy-lint-${dir}";
    inherit (crate) version cargoDeps;
    preBuild = crate.preBuild or "";
    src = fs.toSource {
      root = ../.;
      fileset = fs.union crate.fileset ../rustfmt.toml;
    };
    sourceRoot = "source/${dir}";
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
pkgs.runCommandLocal "nixconvoy-lint" {
  parts = [ py sh (rustLint "switch-guard" switch-guard) ]
    ++ lib.optional (switch-guard-target != null) (rustLint "switch-guard-target" switch-guard-target);
} ''
  touch $out
''

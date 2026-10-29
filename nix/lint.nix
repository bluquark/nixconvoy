# The repo's static checks, cheap enough to gate every deploy that moves
# the pin: ruff and pyrefly over the Python, shellcheck over the plugins'
# hook scripts, rustfmt and clippy over the crate.  The tests: cargo test
# in the crate's build, the suite in nix/tests.nix.
{ pkgs, switch-guard }:
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
pkgs.runCommandLocal "nixconvoy-lint" { parts = [ py sh rustLint ]; } ''
  touch $out
''

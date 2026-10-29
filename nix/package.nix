# The nixconvoy command: the Python package carrying the switch-guard cores
# it drives (src/nixconvoy/guard.py finds them beside itself, so the two
# never pair with another tree's build), with mergiraf, which no
# distribution packages, on its PATH (git and ssh are the machine's own),
# `git review` beside it and the shell prompt segment under share/.
#
# The package is that build behind a symlink farm (nixconvoy-tested) whose
# dependencies hold the test suite's pass (nix/tests.nix): what `nix run`
# runs and the cache carries is tested.  passthru.unchecked is the build
# itself, for a system that runs the suite through system.checks instead,
# its closure built meanwhile; passthru.tests is the suite.
{ pkgs, switch-guard }:
let
  inherit (pkgs) lib;
  python = pkgs.python3;
  # The switch kit's sources (nix/kit.nix and the crate), linked beside the
  # package: a deploy evaluates them against each target's own nixpkgs
  # (memo.kit_src), so the helper a target runs is this very tree's.
  kitSrc = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [ ../nix/kit.nix ../nix/switch-guard.nix switch-guard.fileset ];
  };
  unchecked = python.pkgs.buildPythonApplication {
    pname = "nixconvoy";
    version = "0.1.0";
    pyproject = true;
    src = lib.fileset.toSource {
      root = ../.;
      fileset = lib.fileset.unions [
        ../pyproject.toml
        ../src
        ../integrations/claude-code/bin/git-review
        ../integrations/claude-code/hooks
        ../integrations/shell/prompt.bash
      ];
    };
    build-system = [ python.pkgs.setuptools ];
    doCheck = false;
    makeWrapperArgs = [
      "--suffix" "PATH" ":" (lib.makeBinPath [ pkgs.mergiraf ])
    ];
    postInstall = ''
      install -Dm755 integrations/claude-code/bin/git-review $out/bin/git-review
      install -Dm644 integrations/shell/prompt.bash $out/share/nixconvoy/prompt.bash
      mkdir $out/${python.sitePackages}/nixconvoy/bin
      ln -s ${switch-guard}/bin/nixconvoy-plan ${switch-guard}/bin/nixconvoy-switch $out/${python.sitePackages}/nixconvoy/bin/
      ln -s ${kitSrc} $out/${python.sitePackages}/nixconvoy/kit
    '';
    passthru = { inherit switch-guard kitSrc; unchecked = unchecked; };
    meta.mainProgram = "nixconvoy";
  };
  # The suite reads /proc and flocks: Linux alone.
  tests = import ./tests.nix { inherit pkgs python; nixconvoy = unchecked; };
in
if !pkgs.stdenv.hostPlatform.isLinux then unchecked else
pkgs.symlinkJoin {
  name = "nixconvoy-tested-${unchecked.version}";
  paths = [ unchecked ];
  inherit tests;
  # A cache holds the farm without the suite's output: substituted, not rebuilt.
  allowSubstitutes = true;
  passthru = { inherit unchecked tests switch-guard kitSrc; };
  inherit (unchecked) meta;
}

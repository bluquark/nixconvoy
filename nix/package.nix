# The nixconvoy command: the Python package carrying the switch-guard cores
# it drives (src/nixconvoy/guard.py finds them beside itself, so the two
# never pair with another tree's build), with mergiraf, which no
# distribution packages, on its PATH (git and ssh are the machine's own),
# `git review` beside it and the shell prompt segment under share/.  The test suite (tests/switch_test.py) runs against the
# installed package as its check phase, so a deploy that moves the pin
# pays it in its build, never in the precheck (nix/lint.nix).
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
in
python.pkgs.buildPythonApplication {
  pname = "nixconvoy";
  version = "0.1.0";
  pyproject = true;
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../src
      ../tests
      ../README.md
      ../integrations/claude-code/bin/git-review
      ../integrations/claude-code/hooks
      ../integrations/shell/prompt.bash
    ];
  };
  build-system = [ python.pkgs.setuptools ];
  # The suite reads /proc and flocks: Linux alone.
  doCheck = pkgs.stdenv.hostPlatform.isLinux;
  nativeCheckInputs = [ pkgs.git pkgs.mergiraf pkgs.jq pkgs.util-linux ];
  checkPhase = ''
    runHook preCheck
    HOME=$TMPDIR PYTHONPATH=$out/${python.sitePackages} python3 tests/switch_test.py
    runHook postCheck
  '';
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
  passthru = { inherit switch-guard kitSrc; };
  meta.mainProgram = "nixconvoy";
}

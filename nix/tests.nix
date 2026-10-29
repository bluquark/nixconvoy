# The test suite (tests/switch_test.py) against a build of the package,
# a derivation of its own: the package's symlink farm depends on it
# (nix/package.nix), a NixOS system runs it through system.checks.
{ pkgs, python, nixconvoy }:
let
  fs = pkgs.lib.fileset;
  src = fs.toSource {
    root = ../.;
    fileset = fs.unions [
      (fs.fileFilter (f: f.hasExt "py") ../tests)
      ../integrations/claude-code/hooks
    ];
  };
in
pkgs.runCommand "nixconvoy-tests" {
  nativeBuildInputs = [ python pkgs.git pkgs.mergiraf pkgs.jq pkgs.util-linux ];
} ''
  cd ${src}
  HOME=$TMPDIR PYTHONPATH=${nixconvoy}/${python.sitePackages} python3 tests/switch_test.py
  touch $out
''

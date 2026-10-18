# The nixconvoy command: the Python package wrapped with the switch-guard
# binaries it drives and the tools its subprocesses need, and `git review`
# beside it.
{ pkgs, switch-guard }:
let
  inherit (pkgs) lib;
  python = pkgs.python3;
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
      ../README.md
      ../integrations/claude-code/bin/git-review
    ];
  };
  build-system = [ python.pkgs.setuptools ];
  doCheck = false;
  makeWrapperArgs = [
    "--set" "NIXCONVOY_GUARD_DIR" "${switch-guard}/bin"
    "--prefix" "PATH" ":" (lib.makeBinPath [ pkgs.git pkgs.openssh pkgs.mergiraf pkgs.util-linux ])
  ];
  postInstall = ''
    install -Dm755 integrations/claude-code/bin/git-review $out/bin/git-review
  '';
  passthru = { inherit switch-guard; };
  meta.mainProgram = "nixconvoy";
}

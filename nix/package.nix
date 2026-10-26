# The nixconvoy command: the Python package carrying the switch-guard cores
# it drives (src/nixconvoy/guard.py finds them beside itself, so the two
# never pair with another tree's build), wrapped with the tools its
# subprocesses need, and `git review` beside it.
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
    "--prefix" "PATH" ":" (lib.makeBinPath [ pkgs.git pkgs.openssh pkgs.mergiraf pkgs.util-linux ])
  ];
  postInstall = ''
    install -Dm755 integrations/claude-code/bin/git-review $out/bin/git-review
    mkdir $out/${python.sitePackages}/nixconvoy/bin
    ln -s ${switch-guard}/bin/nixconvoy-plan ${switch-guard}/bin/nixconvoy-switch $out/${python.sitePackages}/nixconvoy/bin/
  '';
  passthru = { inherit switch-guard; };
  meta.mainProgram = "nixconvoy";
}

# The nixconvoy command: the Python package wrapped with the switch-guard
# binaries it drives and the tools its subprocesses need.
{ pkgs, switch-guard }:
let
  inherit (pkgs) lib;
  python = pkgs.python3;
  app = python.pkgs.buildPythonApplication {
    pname = "nixconvoy";
    version = "0.1.0";
    pyproject = true;
    src = lib.fileset.toSource {
      root = ../.;
      fileset = lib.fileset.unions [ ../pyproject.toml ../src ../README.md ];
    };
    build-system = [ python.pkgs.setuptools ];
    doCheck = false;
  };
in
pkgs.symlinkJoin {
  name = "nixconvoy";
  paths = [ app ];
  nativeBuildInputs = [ pkgs.makeWrapper ];
  postBuild = ''
    wrapProgram $out/bin/nixconvoy \
      --set NIXCONVOY_GUARD_DIR ${switch-guard}/bin \
      --prefix PATH : ${lib.makeBinPath [ pkgs.git pkgs.openssh pkgs.mergiraf pkgs.util-linux ]}
  '';
  passthru = { inherit app switch-guard; };
  meta.mainProgram = "nixconvoy";
}

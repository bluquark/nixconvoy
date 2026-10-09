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
  # `git review`, which `nixconvoy review` execs.
  integrations = pkgs.runCommand "nixconvoy-integrations" { } ''
    mkdir -p $out/bin
    cp ${../integrations/claude-code/bin}/* $out/bin/
    chmod +x $out/bin/*
  '';
in
pkgs.symlinkJoin {
  name = "nixconvoy";
  paths = [ app integrations ];
  nativeBuildInputs = [ pkgs.makeWrapper ];
  postBuild = ''
    wrapProgram $out/bin/nixconvoy \
      --set NIXCONVOY_GUARD_DIR ${switch-guard}/bin \
      --prefix PATH : $out/bin:${lib.makeBinPath [ pkgs.git pkgs.openssh pkgs.mergiraf pkgs.util-linux ]}
  '';
  passthru = { inherit app switch-guard; };
  meta.mainProgram = "nixconvoy";
}

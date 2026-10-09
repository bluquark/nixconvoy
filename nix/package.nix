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
  # `git review`, which `nixconvoy review` execs, and the Claude Code
  # plugin (hooks, prompt segment) under share/, for an install by path.
  integrations = pkgs.runCommand "nixconvoy-integrations" { } ''
    mkdir -p $out/bin $out/share/nixconvoy
    cp ${../integrations/claude-code/bin}/* $out/bin/
    chmod +x $out/bin/*
    cp -r ${../integrations/claude-code} $out/share/nixconvoy/claude-code
    chmod +x $out/share/nixconvoy/claude-code/hooks/*.sh $out/share/nixconvoy/claude-code/bin/*
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

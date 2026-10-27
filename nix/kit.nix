# The switch kit a deploy pushes to a target beside its toplevel (memo.py
# evaluates this against the target's own pkgs and config, the switch step
# roots it under /var/lib/nixconvoy/kits by the toplevel's name): the confirmed-switch helper
# nixconvoy-switch drives, its settings and the target's checks.  So a
# target carries nothing of nixconvoy's in its configuration, and the helper
# is always the build the deploying nixconvoy was built from.
{ pkgs, config, deadline, internetCheck, checks }:
let
  inherit (pkgs) lib;
  switchGuard = import ./switch-guard.nix { inherit pkgs; };
  # Where the helper keeps the armed switch's record (`previous` names the
  # system being replaced; boot-assessment.nix reads it by this same path).
  state = "/run/confirmed-switch";
  # The checks' PATH: what a boot's gate (no login PATH) must find there too.
  tools = [ pkgs.coreutils pkgs.util-linux config.systemd.package pkgs.curl config.nix.package
            pkgs.iproute2 pkgs.gnugrep pkgs.gnused pkgs.gawk pkgs.findutils ];
  # The `check` verb's program, and boot-assessment's gate's on a counted
  # boot (the booted system's kit's /checks).
  checksApp = pkgs.writeShellApplication {
    name = "confirmed-switch-checks";
    bashOptions = [ "errexit" "nounset" "pipefail" ];
    runtimeInputs = tools;
    text = checks;
  };
  conf = pkgs.writeText "confirmed-switch.conf" ''
    deadline = ${toString deadline}
    internet_check = ${lib.boolToString internetCheck}
    checks = ${lib.getExe checksApp}
    state_dir = ${state}
    systemctl = ${config.systemd.package}/bin/systemctl
    systemd_run = ${config.systemd.package}/bin/systemd-run
    nix_env = ${config.nix.package}/bin/nix-env
    logger = ${pkgs.util-linux}/bin/logger
    curl = ${lib.getExe pkgs.curl}
  '';
  # Usage: confirmed-switch arm | activate <toplevel> | confirm <toplevel>
  # [reboot-pending] | rollback | check | deadline, driven by nixconvoy's
  # switch step as root (switch-guard/src/switch/core.rs holds the
  # protocol, the lock and the record it keeps in ${state}/state).
  helper = pkgs.writeShellApplication {
    name = "confirmed-switch";
    bashOptions = [ "errexit" "nounset" "pipefail" ];
    text = ''exec ${switchGuard}/bin/nixconvoy-confirmed-switch --config ${conf} "$@"'';
  };
in
pkgs.runCommandLocal "nixconvoy-kit" { } ''
  mkdir $out
  ln -s ${lib.getExe helper} $out/confirmed-switch
  ${lib.optionalString (checks != "") "ln -s ${lib.getExe checksApp} $out/checks"}
''

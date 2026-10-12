{ config, lib, pkgs, ... }:
let
  cfg = config.nixconvoy.confirmedSwitch;
  state = "/run/confirmed-switch";
  tools = [ pkgs.coreutils pkgs.util-linux config.systemd.package pkgs.curl config.nix.package ];
  # The `check` verb and boot-assessment's gate run this same program.
  checks = pkgs.writeShellApplication {
    name = "confirmed-switch-checks";
    bashOptions = [ "errexit" "nounset" "pipefail" ];
    runtimeInputs = tools;
    text = cfg.checks;
  };
  conf = pkgs.writeText "confirmed-switch.conf" ''
    deadline = ${toString cfg.deadline}
    checks = ${lib.getExe checks}
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
    text = ''exec ${config.nixconvoy.switchGuardPackage}/bin/confirmed-switch --config ${conf} "$@"'';
  };
in
{
  # Confirmed switches, the "commit confirmed" of network gear: a toplevel
  # carrying /confirmed-switch tells nixconvoy to activate it with
  # `switch-to-configuration test` under a dead-man timer, verify from the
  # deploying machine, and only then set the profile and bootloader entry.
  # The timer covers a switch that cut its own path in, or a deploying
  # machine that died; no reboot is ever automatic.  The helper is linked
  # into the toplevel rather than put on PATH so the new closure carries it
  # before the target has switched once.
  options.nixconvoy.confirmedSwitch = {
    enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Switch this target confirmed (see the module).  Off, nixconvoy switches it plainly, as nixos-rebuild switch would.";
    };
    deadline = lib.mkOption {
      type = lib.types.ints.positive;
      default = 120;
      description = "Seconds after arming before an unconfirmed switch is rolled back: the activation plus nixconvoy's verification must fit in it (nixconvoy-switch neither starts a verification round nor pauses into its last 5 s, and ends every probe 2 s short of it).";
    };
    checks = lib.mkOption {
      type = lib.types.lines;
      default = "";
      description = "Bash run by the helper's `check` verb (as root, errexit) after the internet probe: the target's own assertions on the new system, whose last stderr line is what a failed verification reports.";
    };
    checksCommand = lib.mkOption {
      type = lib.types.package;
      default = checks;
      readOnly = true;
      description = "`checks` as one program on the helper's PATH (errexit): what the `check` verb runs after the internet probe, and what boot-assessment's gate runs on a counted boot.";
    };
    stateDir = lib.mkOption {
      type = lib.types.str;
      default = state;
      readOnly = true;
      description = "Where the helper keeps the armed switch's record (`state`, written by rename under the lock); its `previous` names the system being replaced (boot-assessment spares it).";
    };
  };

  config = lib.mkIf cfg.enable {
    system.systemBuilderCommands = ''
      ln -s ${lib.getExe helper} $out/confirmed-switch
    '';
  };
}

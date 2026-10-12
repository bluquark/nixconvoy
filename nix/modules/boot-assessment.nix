{ config, options, lib, pkgs, ... }:
let
  cfg = config.nixconvoy.bootAssessment;
  cs = config.nixconvoy.confirmedSwitch;
  hostname = config.networking.hostName;
  # The judge raises a notice on a boot it is about to reboot out of, where
  # a publisher's network timeouts would keep the box down that much
  # longer.  A backstop over ntfy_notice.py's own 3 s per server.
  noticeTimeout = 10;
  esp = config.boot.loader.efi.efiSysMountPoint;
  # systemd-boot sets this only when it booted a counted entry, naming the
  # stub it just decremented: its presence is what "under assessment" means,
  # for the units here and for systemd-bless-boot's own generator.
  countPath = "/sys/firmware/efi/efivars/LoaderBootCountPath-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f";
  initialTries = 2;
  sshPort = lib.head (config.services.openssh.ports or [ 22 ]);
  noNotice = pkgs.writeShellScriptBin "boot-assessment-no-notice" "exit 0";
  statusFile = "/run/boot-assessment/status";
  profileDir = "/nix/var/nix/profiles";

  # What a switch must prove, a boot must too: the very program the
  # confirmed-switch helper's `check` verb runs, reporting its last stderr
  # line.
  confirmed = options ? nixconvoy.confirmedSwitch && cs.enable;
  switchChecks = lib.optionalString confirmed cs.checks;

  # The gate's poll: the box's own reachability and the target's own checks, one
  # failure per line on stdout (everything else they print goes to stderr).
  ownChecks = pkgs.writeShellApplication {
    name = "boot-assessment-checks";
    runtimeInputs = [ pkgs.coreutils pkgs.iproute2 pkgs.gnugrep pkgs.systemd ];
    bashOptions = [ "nounset" ];
    text = ''
      exec 3>&1 1>&2
      fail() { printf '%s\n' "$1" >&3; }
      [ -n "$(ss -Hltn 'sport = :${toString sshPort}')" ] || fail "sshd not listening on ${toString sshPort}"
      ${lib.concatMapStringsSep "\n" (u: ''systemctl is-active -q ${u} || fail "${u} not active"'') cfg.requiredUnits}
      ${cfg.checks}
      exit 0
    '';
  };

  # lanzaboote's installer over the given generation links.
  lzbt = pkgs.writeShellApplication {
    name = "boot-assessment-lzbt";
    bashOptions = [ "errexit" "nounset" "pipefail" ];
    text = ''
      export PATH=${config.systemd.package}/lib/systemd:$PATH
      ${config.boot.lanzaboote.installCommand} \
        ${lib.escapeShellArgs [ "--public-key=${toString config.boot.lanzaboote.publicKeyFile}" "--private-key=${toString config.boot.lanzaboote.privateKeyFile}" ]} \
        ${esp} "$@"
    '';
  };

  # switch-guard's boot-assessment: src/boot/plan.rs is the bootable-set
  # policy (target, booted, running and previous; one anchor per tier - the
  # generation booted at the newest boot at least that many days old; the
  # newest blessed stubs up to `bootable`; never-proven and rejected counted
  # stubs dropped; the ESP fit keeping `reserve` free), src/boot/gate.rs the
  # counted boot's judgement.
  conf = pkgs.writeText "boot-assessment.conf" ''
    esp = ${esp}
    profile_dir = ${profileDir}
    boot_log = ${bootLog}
    state_dir = ${lib.optionalString confirmed cs.stateDir}
    bootable = ${toString bootable}
    tiers = ${toString tiers}
    reserve = ${toString espReserve}
    prune_days = ${toString pruneDays}
    lzbt = ${lib.getExe lzbt}
    nix_env = ${config.nix.package}/bin/nix-env
    systemctl = ${config.systemd.package}/bin/systemctl
    busctl = ${config.systemd.package}/bin/busctl
    date = ${pkgs.coreutils}/bin/date
    notice = ${lib.getExe cfg.notice}
    notice_timeout = ${toString noticeTimeout}
    hostname = ${hostname}
    deadline = ${toString deadline}
    grace = ${toString grace}
    runtime_watchdog = ${toString runtimeWatchdogSec}
    panic = ${toString panicSec}
    status_file = ${statusFile}
    count_path = ${countPath}
    checks = ${lib.getExe ownChecks}
    switch_check = ${lib.optionalString (switchChecks != "") (lib.getExe cs.checksCommand)}
    initial_tries = ${toString initialTries}
    reported = ${reported}
  '';
  run = verb: "${config.nixconvoy.switchGuardPackage}/bin/nixconvoy-boot-assessment --config ${conf} ${verb}";

  # Seconds since stage 2 began: an initrd that waited for a manual LUKS
  # unlock (or a slow fsck) must not eat into a counted boot's deadline.
  deadline = 300;
  # The watch reboots on the gate's verdict; its own deadline, this much
  # later, only covers a gate that never ran.
  grace = 60;
  runtimeWatchdogSec = 60;
  panicSec = 30;
  bootable = 15;
  tiers = [ 1 7 30 ];
  bootLog = "/var/lib/boot-assessment/boots";
  # Kept free on the ESP by the fit check: systemd-boot, the stubs, slack.
  espReserve = 32 * 1024 * 1024;
  # A time-based nix.gc would drop the blessed fallback once it is old,
  # whatever the newer generations' state: the prune spares the anchors and
  # what the failover needs instead.
  pruneDays = lib.last tiers;
  reported = "/run/boot-assessment/reported";

  # Runs in place of lanzaboote's installer, for the toplevel
  # switch-to-configuration boot (or a confirmed switch's confirm) installs.
  install = lib.getExe (pkgs.writeShellApplication {
    name = "boot-assessment-install";
    bashOptions = [ "errexit" "nounset" "pipefail" ];
    text = ''exec ${run "install"} "$1"'';
  });

  panic = lib.getExe (pkgs.writeShellApplication {
    name = "boot-assessment-panic";
    bashOptions = [ ];
    text = "echo ${toString panicSec} > /proc/sys/kernel/panic";
  });
in
{
  options.nixconvoy.bootAssessment = {
    enable = lib.mkEnableOption "systemd-boot boot counting as a failover to the last proven generation (lanzaboote installs the counted stubs)";
    requiredUnits = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "tailscaled.service" ];
      description = "Units the gate requires active for a boot to pass, beside sshd listening; the gate also orders itself after them.";
    };
    checks = lib.mkOption {
      type = lib.types.lines;
      default = "";
      description = "Bash inlined into the gate's poll loop (as root, nounset, not errexit), before the confirmed-switch checks: the target's own assertions on a good boot, each naming its failure through `fail <reason>` so a poll reports every failing one.";
    };
    notice = lib.mkOption {
      type = lib.types.package;
      default = noNotice;
      defaultText = lib.literalExpression "a no-op";
      description = "The command the judge and the report raise the boot-assessment, boot-failover and esp-full notices through, under a ${toString noticeTimeout} s timeout: `set <name>` with the body on stdin, `clear <name>`.  The default discards them; point it at whatever reaches you (an ntfy push, a login banner).";
    };
    noticeEnvironmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "An EnvironmentFile for the units that run the notice command (a notice command that publishes on its own needs its credentials).";
    };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [ {
    # systemd-boot's automatic boot assessment as a failover to the last proven
    # generation (systemd.io/AUTOMATIC_BOOT_ASSESSMENT).  Once a stub's tries
    # suffix hits 0, systemd-boot skips it when resolving its "nixos-*"
    # default.  systemd-bless-boot removes the counter once
    # boot-complete.target is reached, which the gate makes mean "reachable
    # over the LAN".  An uncounted boot never reboots itself: a blessed
    # generation that breaks later is hardware or filesystem trouble, left
    # for someone to look at.
    system.build.installBootLoader = lib.mkForce install;
    assertions = [
      {
        assertion = options ? boot.lanzaboote;
        message = "nixconvoy.bootAssessment needs lanzaboote (it installs the counted stubs through lzbt)";
      }
      {
        assertion = !(options ? boot.lanzaboote) || (!config.boot.lanzaboote.measuredBoot.enable && config.boot.lanzaboote.extraEfiSysMountPoints == [ ]);
        message = "nixconvoy.bootAssessment runs lzbt itself: one ESP, no measured boot";
      }
    ];

    nix.gc.options = lib.mkForce "";
    systemd.services.nix-gc.serviceConfig.ExecStartPre = run "prune";

    # Reboot-on-panic for the initrd of a counted boot only: the kernel keeps
    # its default of hanging (kernel.panic=0) everywhere else.  The hardware
    # watchdog cannot be armed here - no D-Bus in stage 1 - so an initrd hang
    # that is not a panic waits for someone.  The initrd's closure does not
    # follow a unit's ExecStart on its own.
    boot.initrd.systemd.storePaths = [ panic ];
    boot.initrd.systemd.services.boot-assessment-panic = {
      description = "Under boot assessment: reboot on a kernel panic";
      wantedBy = [ "sysinit.target" ];
      unitConfig = {
        DefaultDependencies = false;
        ConditionPathExists = countPath;
      };
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${panic}";
      };
    };

    # The judge: arms the hardware watchdog and reboot-on-panic as soon as
    # D-Bus is up, reboots when the gate fails (or never runs), and disarms
    # once systemd-bless-boot has removed the counter - the target being
    # reached is not enough, as a failed rename would leave the tries to be
    # spent.  Independent of the rest of the boot, so a unit that hangs
    # before the gate still meets the deadline.
    systemd.services.boot-assessment-watch = {
      description = "Reboot a counted boot that is not blessed within the deadline";
      wantedBy = [ "multi-user.target" ];
      wants = [ "dbus.service" ];
      after = [ "dbus.service" ];
      before = [ "shutdown.target" ];
      conflicts = [ "shutdown.target" ];
      unitConfig = {
        DefaultDependencies = false;
        ConditionPathExists = countPath;
      };
      path = [ pkgs.coreutils pkgs.systemd ];
      restartIfChanged = false;
      serviceConfig = {
        Type = "exec";
        ExecStart = run "watch";
        EnvironmentFile = lib.mkIf (cfg.noticeEnvironmentFile != null) cfg.noticeEnvironmentFile;
      };
    };

    # Only the box's own reachability and services - nothing that depends
    # on another machine answering, so a neighbour's outage cannot condemn a
    # generation.  Pulled in only on counted boots, through
    # boot-complete.target.
    systemd.services.boot-assessment = {
      description = "Judge this boot: reachable on the LAN, the target's own checks pass";
      requiredBy = [ "boot-complete.target" ];
      before = [ "boot-complete.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" "sshd.service" ] ++ cfg.requiredUnits;
      path = [ pkgs.coreutils pkgs.iproute2 pkgs.systemd ];
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = run "gate";
      };
    };

    # Once per boot, and only before any switch (the stamp, plus the
    # current-vs-booted check for the activation that first starts the unit,
    # keep a deploy's advance of the profile from reading as a failover):
    # log the boot for the anchors, then flag a boot that did not land on
    # the profile's newest generation.  The watch's boot-assessment notice
    # is cleared only when the newest generation passes, so a counted
    # fallback passing does not erase the failed boot's reasons.
    systemd.services.boot-assessment-report = {
      description = "Raise the boot-failover notice when not running the newest generation";
      wantedBy = [ "multi-user.target" ];
      # The notice may be a push off the box.
      wants = [ "network-online.target" ];
      after = [ "local-fs.target" "network-online.target" ];
      unitConfig.ConditionPathExists = "!${reported}";
      path = [ pkgs.coreutils ];
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        StateDirectory = baseNameOf (dirOf bootLog);
        ExecStart = run "report";
        EnvironmentFile = lib.mkIf (cfg.noticeEnvironmentFile != null) cfg.noticeEnvironmentFile;
      };
    };
  } (lib.optionalAttrs (options ? boot.lanzaboote) {
    boot.lanzaboote.bootCounting.initialTries = initialTries;
    boot.lanzaboote.configurationLimit = bootable;
  }) ]);
}

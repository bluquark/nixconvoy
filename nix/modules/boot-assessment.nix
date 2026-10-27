{ config, lib, pkgs, ... }:
let
  cfg = config.nixconvoy.bootAssessment;
  # The switch kits nixconvoy roots, one per system by its store path's
  # name (nix/kit.nix, targets.KIT_DIR): the booted system's checks are the
  # switch's own, run on a counted boot too when its kit carries any.
  kits = "/var/lib/nixconvoy/kits";
  # The judge raises a notice on a boot it is about to reboot out of, where
  # a publisher's network timeouts would keep the box down that much
  # longer.  A backstop over ntfy_notice.py's own 3 s per server.
  noticeTimeout = 10;
  esp = config.boot.loader.efi.efiSysMountPoint;
  # systemd-boot sets this only when it booted a counted entry, naming the
  # stub it just decremented: its presence is what "under assessment" means,
  # for the units here and for systemd-bless-boot's own generator.
  countPath = "/sys/firmware/efi/efivars/LoaderBootCountPath-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f";
  sshPort = lib.head config.services.openssh.ports;
  panicSec = 30;
  tiers = [ 1 7 30 ];
  # lanzaboote's own options, defaulted below: the stubs it installs are
  # the bootable set, each counted with that many tries.
  limit = config.boot.lanzaboote.configurationLimit;
  bootable = if limit == null then 0 else limit;
  initialTries = config.boot.lanzaboote.bootCounting.initialTries;
  minBootable = 4 + lib.length tiers;
  # nix's legacy parser expands combined short flags (-vd is -v -d) and the
  # options are inlined into a bash script, which unquotes them.
  gcWords = lib.filter lib.isString (builtins.split "[[:space:]]+" (lib.replaceStrings [ "'" "\"" ] [ "" "" ] config.nix.gc.options));
  # The newest generations the prune spares beside the bootable set, for a
  # rollback by hand to a never-booted one: the agent checkouts and the
  # nightly make several deploys a day, so this is about a day's.
  keepNewest = 10;
  bootLog = "/var/lib/boot-assessment/boots";
  reported = "/run/boot-assessment/reported";

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
  # - reserve: kept free on the ESP by the fit check: systemd-boot, the
  #   stubs, slack.
  # - keep_newest: the prune keeps the bootable set an install would choose
  #   for the profile's generation, and this many newest generations.
  # - deadline: the gate fails as soon as nothing is left starting (each
  #   job is bounded by its own start timeout), so this only bounds a boot
  #   held up by a unit with an infinite one.  Seconds since stage 2 began:
  #   an initrd that waited for a manual LUKS unlock (or a slow fsck) must
  #   not eat into it.  The watch reboots on the gate unit's failure, and on
  #   the deadline only while the gate has not started.
  # - state_dir: the confirmed-switch helper's record (nix/kit.nix's
  #   state), whose `previous` the planner spares.
  # - kits_dir: what a switch must prove, a boot must too: the very
  #   program the confirmed-switch helper's `check` verb runs, at
  #   <kits_dir>/<booted system's name>/checks, reporting its last stderr
  #   line; skipped when the booted system has no kit, or one without checks.
  conf = pkgs.writeText "boot-assessment.conf" ''
    esp = ${esp}
    profile_dir = /nix/var/nix/profiles
    boot_log = ${bootLog}
    state_dir = /run/confirmed-switch
    bootable = ${toString bootable}
    tiers = ${toString tiers}
    reserve = ${toString (32 * 1024 * 1024)}
    keep_newest = ${toString keepNewest}
    lzbt = ${lib.getExe lzbt}
    nix_env = ${config.nix.package}/bin/nix-env
    systemctl = ${config.systemd.package}/bin/systemctl
    busctl = ${config.systemd.package}/bin/busctl
    date = ${pkgs.coreutils}/bin/date
    notice = ${lib.getExe cfg.notice}
    notice_timeout = ${toString noticeTimeout}
    hostname = ${config.networking.hostName}
    deadline = ${toString cfg.deadline}
    runtime_watchdog = 60
    panic = ${toString panicSec}
    status_file = /run/boot-assessment/status
    count_path = ${countPath}
    checks = ${lib.getExe ownChecks}
    kits_dir = ${kits}
    initial_tries = ${toString initialTries}
    reported = ${reported}
  '';
  run = verb: "${cfg.package}/bin/nixconvoy-boot-assessment --config ${conf} ${verb}";

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
    package = lib.mkOption {
      type = lib.types.package;
      description = "The switch-guard build whose nixconvoy-boot-assessment binary this target runs; the flake's nixosModules.boot-assessment sets it to its own.";
    };
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
      default = pkgs.writeShellScriptBin "boot-assessment-no-notice" "exit 0";
      defaultText = lib.literalExpression "a no-op";
      description = "The command the judge and the report raise the boot-assessment, boot-failover and esp-full notices through, under a ${toString noticeTimeout} s timeout: `set <name>` with the body on stdin, `clear <name>`.  The default discards them; point it at whatever reaches you (an ntfy push, a login banner).";
    };
    deadline = lib.mkOption {
      type = lib.types.ints.positive;
      default = 900;
      description = "Seconds after stage 2 began by which a counted boot whose checks still fail is rebooted even though a unit is still starting: the backstop for units with an infinite start timeout.  Checks still failing once nothing is left starting fail the boot at once.";
    };
    noticeEnvironmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = "An EnvironmentFile for the units that run the notice command (a notice command that publishes on its own needs its credentials).";
    };
  };

  config = lib.mkIf cfg.enable {
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
        assertion = !config.boot.lanzaboote.measuredBoot.enable && config.boot.lanzaboote.extraEfiSysMountPoints == [ ];
        message = "nixconvoy.bootAssessment needs lanzaboote with one ESP and no measured boot: it installs the counted stubs through lzbt itself";
      }
      {
        assertion = bootable >= minBootable;
        message = "nixconvoy.bootAssessment: boot.lanzaboote.configurationLimit sizes the bootable set, which holds the target, booted, running and previous generations and ${toString (lib.length tiers)} anchors: at least ${toString minBootable}, not ${if limit == null then "null (unlimited)" else toString limit}";
      }
      {
        assertion = initialTries > 0;
        message = "nixconvoy.bootAssessment judges counted boots: boot.lanzaboote.bootCounting.initialTries must be above 0";
      }
      {
        assertion = config.nix.gc.automatic;
        message = "nixconvoy.bootAssessment prunes generations as nix-gc's pre-start: nix.gc.automatic must be on";
      }
      {
        assertion = !lib.any (w: builtins.match "-[a-zA-Z]*d[a-zA-Z]*" w != null || lib.hasPrefix "--delete-old" w) gcWords;
        message = "nixconvoy.bootAssessment's prune decides which generations go (the bootable set, the anchors and the newest stay): nix.gc.options must not delete generations (-d, --delete-old, --delete-older-than) - set it to \"\"";
      }
    ];
    # Below mkDefault, so a consumer's own default wins and meets the
    # assertions instead of conflicting.
    boot.lanzaboote.bootCounting.initialTries = lib.mkOverride 1200 2;
    boot.lanzaboote.configurationLimit = lib.mkOverride 1200 15;

    # The prune (`prune --dry-run` shows it) decides which profile
    # generations go, on nix.gc's timer.  Just below mkDefault, so another
    # module's default yields while an explicit setting meets the assertion.
    nix.gc.options = lib.mkOverride 999 "";
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
    # D-Bus is up, reboots when the gate fails or will never run, and disarms
    # once systemd-bless-boot has removed the counter - the target being
    # reached is not enough, as a failed rename would leave the tries to be
    # spent.  Independent of the rest of the boot, so a unit that hangs
    # before the gate still meets the deadline.
    systemd.services.boot-assessment-watch = {
      description = "Reboot a counted boot that fails its assessment";
      wantedBy = [ "multi-user.target" ];
      wants = [ "dbus.service" ];
      after = [ "dbus.service" ];
      before = [ "shutdown.target" ];
      conflicts = [ "shutdown.target" ];
      unitConfig = {
        DefaultDependencies = false;
        ConditionPathExists = countPath;
      };
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
      # The confirmed-switch checks may run ip or ss by name: here they find
      # them only through this path (their own runtimeInputs lack iproute2).
      path = [ pkgs.iproute2 ];
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = run "gate";
        # A check that hangs must still fail the unit, which the watch
        # reboots on: past the gate's own deadline, as it starts after
        # stage 2 does.
        TimeoutStartSec = cfg.deadline;
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
      restartIfChanged = false;
      serviceConfig = {
        Type = "oneshot";
        StateDirectory = baseNameOf (dirOf bootLog);
        ExecStart = run "report";
        EnvironmentFile = lib.mkIf (cfg.noticeEnvironmentFile != null) cfg.noticeEnvironmentFile;
      };
    };
  };
}

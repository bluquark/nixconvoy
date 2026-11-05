# The switch kit a deploy pushes to a target beside its toplevel (memo.py
# evaluates this against the target's own pkgs and config, the switch step
# roots it under /var/lib/nixconvoy/kits by the toplevel's name): the confirmed-switch helper
# nixconvoy-switch drives, its settings and the target's checks (`checks`:
# the target_shell statements, each run under `prelude`).  So a target
# carries nothing of nixconvoy's in its configuration, and the helper is
# always the build the deploying nixconvoy was built from.
{ pkgs, config, deadline, internetCheck, prelude, checks }:
let
  inherit (pkgs) lib;
  switchGuard = import ./switch-guard-target.nix { inherit pkgs; };
  # Where the helper keeps the armed switch's record (`previous` names the
  # system being replaced; boot-assessment.nix reads it by this same path).
  state = "/run/confirmed-switch";
  # The checks' PATH: what a boot's gate (no login PATH) must find there too.
  tools = [ pkgs.coreutils pkgs.util-linux config.systemd.package pkgs.curl config.nix.package
            pkgs.iproute2 pkgs.gnugrep pkgs.gnused pkgs.gawk pkgs.findutils ];
  # The `check` verb's program, and boot-assessment's gate's on a counted
  # boot (the booted system's kit's /checks): the target_shell statements
  # each in a root's-shell process of its own, all at once; their output replayed
  # once all have ended, a failing one's stderr before the last line, one
  # naming every failure, which nixconvoy reports.
  # Root's shell (bash when root has none).  Each statement's script is
  # parsed by it at build time, so a typo cannot reach a switch - when it
  # is a store path (a shell given as a path outside the store runs only
  # on the target); under bash, shellcheck reads it too.
  rootShell =
    let s = config.users.users.root.shell;
    in if s == null then lib.getExe pkgs.bashInteractive
       else if lib.isDerivation s then (if s ? shellPath then "${s}${s.shellPath}" else lib.getExe s)
       else toString s;
  inStore = lib.hasPrefix builtins.storeDir rootShell;
  isBash = baseNameOf rootShell == "bash";
  scripts = lib.imap0 (i: c: pkgs.runCommandLocal "confirmed-switch-check-${toString i}" {
    script = "${prelude}\n${c}\n";
    passAsFile = [ "script" ];
    nativeBuildInputs = lib.optional isBash pkgs.shellcheck;
  } ''
    ${lib.optionalString inStore ''${rootShell} -n "$scriptPath"''}
    ${lib.optionalString isBash ''shellcheck --shell=bash "$scriptPath"''}
    cp "$scriptPath" $out
  '') checks;
  # What a failing statement with nothing on stderr is reported by: its
  # first line (held in a file: statement text in the runner's own script
  # would meet its shellcheck).
  firstLine = c: lib.head (lib.filter (l: l != "") (lib.splitString "\n" c) ++ [ "" ]);
  checksApp = pkgs.writeShellApplication {
    name = "confirmed-switch-checks";
    bashOptions = [ "errexit" "nounset" "pipefail" ];
    runtimeInputs = tools;
    text = ''
      scripts=(${lib.concatMapStringsSep " " lib.escapeShellArg scripts})
      mapfile -t names <${pkgs.writeText "confirmed-switch-check-names" (lib.concatMapStrings (c: firstLine c + "\n") checks)}
      [ "''${#scripts[@]}" != 0 ] || exit 0
      replay() { cat "$1"; [ ! -s "$1" ] || [ -z "$(tail -c 1 "$1")" ] || echo; }
      tmp=$(mktemp -d)
      trap 'rm -rf "$tmp"' EXIT
      pids=()
      for i in "''${!scripts[@]}"; do
        ${rootShell} "''${scripts[$i]}" >"$tmp/$i.out" 2>"$tmp/$i.err" </dev/null &
        pids+=("$!")
      done
      failed=""
      for i in "''${!pids[@]}"; do
        code=0
        wait "''${pids[$i]}" || code=$?
        replay "$tmp/$i.out"
        replay "$tmp/$i.err" >&2
        [ "$code" = 0 ] && continue
        last=$(grep -v '^[[:space:]]*$' "$tmp/$i.err" | tail -n 1 | sed 's/[[:space:]]*$//' || true)
        [ -n "$last" ] || last="\"''${names[$i]}\" exited $code"
        failed="$failed; $last"
      done
      [ -z "$failed" ] || { echo "''${failed#; }" >&2; exit 1; }
    '';
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
  ${lib.optionalString (checks != [ ]) "ln -s ${lib.getExe checksApp} $out/checks"}
''

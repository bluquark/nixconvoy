{ lib, ... }:
{
  # The switch policy a host declares for nixconvoy, read in the same eval
  # as its toplevel (nixconvoy's memo.eval_expr), so it costs nothing extra.
  options.nixconvoy = {
    switchGuardPackage = lib.mkOption {
      type = lib.types.package;
      description = "The switch-guard build whose confirmed-switch and boot-assessment binaries this host runs; the flake's nixosModules.default sets it to the same build the nixconvoy command drives.";
    };
    protectedUnits = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "systemd-networkd.service" "nftables.service" "dnsmasq.service" ];
      description = "Units whose stop, restart or reload a deploy never performs unasked: the host is dry-activated first, and a switch that would touch one of these stops the whole deploy unless --danger-network-config-change is given.  For a router, the units that take the LAN or the path in down.";
    };
    switchLast = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Switch this host alone after every other host (everything else still has connectivity while it copies), and verify a confirmed switch of it only while the deploying machine still reaches the internet.  For the router.";
    };
  };
}

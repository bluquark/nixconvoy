{ lib, ... }:
{
  # The switch policy a target declares for nixconvoy, read in the same eval
  # as its toplevel (nixconvoy's memo.eval_expr), so it costs nothing extra.
  options.nixconvoy = {
    switchGuardPackage = lib.mkOption {
      type = lib.types.package;
      description = "The switch-guard build whose nixconvoy-confirmed-switch and nixconvoy-boot-assessment binaries this target runs; the flake's nixosModules.default sets it to the same build the nixconvoy command drives.";
    };
    protected = lib.mkOption {
      type = lib.types.attrsOf (lib.types.submodule {
        options = {
          units = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            description = "The units this protection covers.";
          };
          why = lib.mkOption {
            type = lib.types.str;
            description = "What a stop, restart or reload of one of them risks: the refusal quotes it.";
          };
        };
      });
      default = { };
      example = {
        risky-network-reload-or-restart = {
          units = [ "systemd-networkd.service" "nftables.service" "dnsmasq.service" ];
          why = "risk of breaking essential LAN/WAN functionality";
        };
      };
      description = "Named groups of units whose stop, restart or reload a deploy never performs unasked: the target is dry-activated first, and a switch that would touch one of them stops the whole deploy unless `--allow <name>` is given, the refusal naming the group, its `why` and the exact command that admits it.  For a router, the units that take the LAN or the path in down.";
    };
    switchLast = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Switch this target alone after every other target (everything else still has connectivity while it copies), and verify a confirmed switch of it only while the deploying machine still reaches the internet.  For the router.";
    };
  };
}

{ lib, ... }:
{
  # The switch policy a target declares for nixconvoy, read in the same eval
  # as its toplevel (nixconvoy's memo.eval_expr), so it costs nothing extra.
  options.nixconvoy = {
    switchGuardPackage = lib.mkOption {
      type = lib.types.package;
      description = "The switch-guard build whose nixconvoy-confirmed-switch and nixconvoy-boot-assessment binaries this target runs; the flake's nixosModules.default sets it to the same build the nixconvoy command drives.";
    };
    switchLast = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Switch this target alone after every other target (everything else still has connectivity while it copies), and verify a confirmed switch of it only while the deploying machine still reaches the internet.  For the router.";
    };
  };
}

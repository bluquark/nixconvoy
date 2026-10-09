{
  description = "nixconvoy: clobber-safe NixOS deploys for hosts shared by several checkouts";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
  let
    systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
    forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
  in {
    packages = forAll (pkgs: rec {
      switch-guard = import ./nix/switch-guard.nix { inherit pkgs; };
      nixconvoy = import ./nix/package.nix { inherit pkgs switch-guard; };
      default = nixconvoy;
    });

    apps = forAll (pkgs: {
      default = {
        type = "app";
        program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.nixconvoy}/bin/nixconvoy";
      };
    });

    checks = forAll (pkgs: {
      lint = import ./nix/lint.nix {
        inherit pkgs;
        inherit (self.packages.${pkgs.stdenv.hostPlatform.system}) switch-guard nixconvoy;
      };
    });

    # The host side: the switch policy options nixconvoy reads at eval time,
    # the confirmed-switch helper, and (optionally) boot assessment.
    nixosModules = {
      default = { pkgs, ... }: {
        imports = [ ./nix/modules/options.nix ./nix/modules/confirmed-switch.nix ];
        nixconvoy.switchGuardPackage = nixpkgs.lib.mkDefault
          self.packages.${pkgs.stdenv.hostPlatform.system}.switch-guard;
      };
      boot-assessment = ./nix/modules/boot-assessment.nix;
    };
  };
}

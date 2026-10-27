{
  description = "nixconvoy: clobber-safe NixOS deploys for targets shared by several checkouts";

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

    checks = forAll (pkgs: {
      lint = import ./nix/lint.nix {
        inherit pkgs;
        inherit (self.packages.${pkgs.stdenv.hostPlatform.system}) switch-guard;
      };
      # The package build runs the suite (nix/package.nix): named here so
      # `nix flake check` runs it too.
      tests = self.packages.${pkgs.stdenv.hostPlatform.system}.nixconvoy;
    });

    # The target side is pushed by each deploy (nix/kit.nix); the one module
    # left is boot assessment, whose units live in the system.
    nixosModules.boot-assessment = { pkgs, ... }: {
      imports = [ ./nix/modules/boot-assessment.nix ];
      nixconvoy.bootAssessment.package = nixpkgs.lib.mkDefault
        self.packages.${pkgs.stdenv.hostPlatform.system}.switch-guard;
    };
  };
}

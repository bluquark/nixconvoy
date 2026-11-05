{
  description = "nixconvoy: clobber-safe NixOS deploys for targets shared by several checkouts";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  # Builds of the packages below, pushed by the maintainer;
  # `nix run github:bluquark/nixconvoy` offers them, a flake taking
  # nixconvoy as an input names them in its own nix.conf.
  nixConfig = {
    extra-substituters = [ "https://nixconvoy.cachix.org" ];
    extra-trusted-public-keys = [ "nixconvoy.cachix.org-1:8HCQ3qVkg+lUbouy8s7aWzWoDoGDrm0lkg07VvD8RwQ=" ];
  };

  outputs = { self, nixpkgs }:
  let
    systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
    forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    # The commit the version line names.  As a path input inside another
    # repository's flake, self carries that repository's revision: none then.
    rev = if self.outPath == self.sourceInfo.outPath then self.shortRev or self.dirtyShortRev or null else null;
  in {
    packages = forAll (pkgs: rec {
      switch-guard = import ./nix/switch-guard.nix { inherit pkgs; };
      nixconvoy = import ./nix/package.nix { inherit pkgs switch-guard rev; };
      default = nixconvoy;
    } // nixpkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
      switch-guard-target = import ./nix/switch-guard-target.nix { inherit pkgs; };
    });

    checks = forAll (pkgs: let p = self.packages.${pkgs.stdenv.hostPlatform.system}; in {
      lint = import ./nix/lint.nix { inherit pkgs; inherit (p) switch-guard; switch-guard-target = p.switch-guard-target or null; };
    } // (if pkgs.stdenv.hostPlatform.isLinux
          then { inherit (p.nixconvoy) tests; inherit (p) switch-guard-target; }
          else { package = p.nixconvoy; }));

    # The target side is pushed by each deploy (nix/kit.nix); the one module
    # left is boot assessment, whose units live in the system.
    nixosModules.boot-assessment = { pkgs, ... }: {
      imports = [ ./nix/modules/boot-assessment.nix ];
      nixconvoy.bootAssessment.package = nixpkgs.lib.mkDefault
        self.packages.${pkgs.stdenv.hostPlatform.system}.switch-guard-target;
    };
  };
}

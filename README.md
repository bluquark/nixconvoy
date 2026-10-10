# nixconvoy

Deploy a NixOS flake to a few hosts from several checkouts at once, without
one checkout ever reverting what another already switched.

nixconvoy is for the way a homelab actually evolves: lots of small,
experimental changes, tested in production, kept honest by NixOS's atomic
switches and generations.  You stage a change, run `nixconvoy apply`, and the
hosts that differ from the result are switched.  If the host does not come
back reachable and healthy, the switch rolls itself back.  Only after the
hosts run the change is it committed, so a branch never claims more than
what is deployed.

## The model in git terms

- **A work branch is where experiments live.**  Each checkout you deploy from
  owns one, listed by pattern in `deploy-branches` at the repo root.  A
  deploy commits the staged state onto it and pushes it; nothing else
  writes there.
- **The hosts run the convoy: origin/main plus every open work branch.**
  Each deploy union-merges the pushed work branches (mergiraf resolves
  structural conflicts) and builds that.  Two checkouts changing unrelated
  modules both stay deployed; a real conflict stops the deploy and names
  the branch.
- **Targets are inferred.**  Each host's ground truth is its running
  system.  Whatever differs from the union is switched; a no-op deploy
  evaluates and switches nothing.
- **`land` lands a branch on main**, as one commit whose message is the
  branch's `COMMIT_MSG.md`; **`abandon`** discards it.  Either way the
  checkout stays on its work branch, reset onto the new main, and every
  other checkout on the machine is rebased onto it.

A checkout on `main` (or any branch `deploy-branches` does not list) is a
plain checkout: it deploys the convoy too, catching the hosts up, but owns
no work.

## What keeps it safe

- **Compare-and-swap switches.**  The switch runs under a lock held on the
  target host that first re-checks the host still runs what this deploy
  probed.  A deploy that raced another aborts cleanly instead of reverting
  it, and a re-run converges.
- **Lineage.**  Every host records where its system came from: the union,
  each branch's tip, and the deployer.  A deploy that would overwrite
  another checkout's not-yet-pushed increment is refused (or leaves that
  host alone when its own work does not reach it).  A lineage older than
  30 minutes is a dead deployer's and is taken over with a warning.
- **Confirmed switches.**  With the NixOS module, a switch activates the
  new system with `switch-to-configuration test` under a dead-man timer,
  verifies it from the deploying machine (reachable, on the internet, its
  own checks pass), and only then sets the profile and bootloader entry;
  otherwise the host rolls back to what it ran.  No reboot is ever
  automatic.
- **Protected units.**  A host can declare units a deploy must never
  restart unasked (a router's networkd, nftables, dnsmasq).  The new system
  is dry-activated first; a switch that would touch one stops the whole
  deploy unless `--danger-network-config-change` is given.
- **Boot assessment** (optional, with lanzaboote).  systemd-boot's boot
  counting as a failover to the last proven generation; a generation the
  host fell back from is never switched to again.
- **Commit after switch.**  An increment that fails to evaluate, build or
  switch is never committed anywhere; its changes stay staged for the
  re-run.

## Installation

nixconvoy is a flake.  Add it as an input, import the NixOS module on each
host, and re-export the command from your own flake so the deployer and the
host-side helper are pinned together by your `flake.lock`:

```nix
{
  inputs.nixconvoy = {
    url = "github:bluquark/nixconvoy";
    inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, nixconvoy, ... }: {
    nixosConfigurations.myhost = nixpkgs.lib.nixosSystem {
      modules = [
        nixconvoy.nixosModules.default
        ./hosts/myhost/configuration.nix
      ];
    };
    packages.x86_64-linux.nixconvoy = nixconvoy.packages.x86_64-linux.nixconvoy;
  };
}
```

Then run it from the repo root:

```sh
nix run .#nixconvoy -- apply -m "describe this increment"
```

On a NixOS machine you deploy from, the package in
`environment.systemPackages` puts `nixconvoy` (and `git review`) on PATH.
That is the running system's build, which trails the lock until the
machine itself switches; an unattended deploy that first moves the lock
should run the lock's build (`nix run .#nixconvoy -- apply ...`).

Requirements on each host: `/run/current-system` reachable over ssh with
passwordless sudo for the deploying user (a switch, the lineage file under
`/var/lib/nixconvoy` and a dry activation all need root), and the host's
nixosConfiguration named after its directory under `hosts/`.

## Layout nixconvoy expects

```
deploy-branches     patterns of the work branches (one per line, `*` the wildcard)
nixconvoy.toml      optional settings (below)
COMMIT_MSG.md       on a work branch: the pending landing's commit message
<flake>/flake.nix
<flake>/hosts/<name>/   one directory per nixosConfiguration
```

### nixconvoy.toml

Every key is optional.

```toml
flake = "nixos"              # directory holding flake.nix (default ".")
hosts_dir = "nixos/hosts"    # default "<flake>/hosts"
system = "x86_64-linux"
target = "me@{host}.lan"     # ssh target template (default "{host}")
branch_prefix = "{host}-deploys"   # a worktree under a plain checkout owns "<prefix>-<name>"

[precheck]                   # a flake check proven before any host build
attr = "repo-lint"           # checks.<system>.<attr>
globs = ["nixos/*.nix", "*.py"]   # the files it reads: its memo keys on exactly these

[memo]                       # an overlay whose attributes map 1:1 onto files
overlay = "nixos/pkgs/default.nix"
namespace = "homelab"        # named as pkgs.homelab.<attr>

[eval]
parallel = true              # Determinate Nix's parallel evaluation

[build]
host = { darwin = "builder" }      # realise closures there (per platform)
unreachable_hint = "asleep? wake it first"
public_substituters = "https://cache.nixos.org"
[[build.caches]]             # your own caches, asked first
url = "http://builder.lan:5000"
served_by = "builder"        # skipped when building on that host

[hosts.laptop]
local_only = true            # deployable only from itself
target = "root@10.0.0.9"     # overrides the template
tags = ["portable"]          # `@portable` on the command line names every host tagged so
```

### The NixOS module

```nix
nixconvoy.confirmedSwitch.deadline = 120;   # seconds before an unconfirmed switch rolls back
nixconvoy.confirmedSwitch.checks = ''       # bash, as root, errexit: the host's own assertions
  systemctl is-active -q nginx.service
'';
nixconvoy.protectedUnits = [ "systemd-networkd.service" "nftables.service" ];
nixconvoy.switchLast = true;                # the router: switched alone, after everything else
```

`nixconvoy.nixosModules.boot-assessment` adds `nixconvoy.bootAssessment.*`
(requires lanzaboote).

## Commands

```
nixconvoy apply [-m "msg"] [--reset] [--take-over host] [host|@tag ...]   deploy
nixconvoy apply --dry-activate [host|@tag ...]   build and copy, then only report what each switch would do
nixconvoy commit -m "msg"      commit + push the staged state, no build or switch
nixconvoy land [name ...]      land the work branch on main
nixconvoy abandon [name ...]   discard it
nixconvoy prune [name ...]     remove the finished subagents' worktrees holding nothing of their own
nixconvoy pull                 rebase every checkout on this machine onto origin/main
nixconvoy status               every checkout on this machine, and origin's other branches
nixconvoy review               the pending landing: COMMIT_MSG.md, then the diff
nixconvoy exec [host|@tag ...] -- cmd [arg ...]   run a command on each host, concurrently
```

`nixconvoy --help` and the docstring of `src/nixconvoy/cli.py` describe each
step of a deploy in full.

## Worktrees

A git worktree under `<checkout>/.claude/worktrees/<name>` (the layout
Claude Code's worktree isolation creates) owns `<checkout's branch>-<name>`
from its first deploy, deploys in parallel with its parent, and is removed
with its branch by `land` or `abandon` once its task is done - or by
`prune` when it holds nothing of its own and its agent has stopped (a
resume of the agent recreates it) or the session that spawned the agent
has exited.  `integrations/claude-code/` is a Claude Code plugin holding
the hooks that create such worktrees under the spawning checkout, prune
them when their agent stops and recreate them for its resume, beside the
`git review` command and a bash prompt segment:

```
/plugin install nixconvoy --marketplace bluquark/nixconvoy
```

The package also ships the plugin tree under `share/nixconvoy/claude-code/`
for a repo that wires the hooks itself: a settings.json pointing at
`hooks/worktree-create.sh`, `hooks/worktree-remove.sh`,
`hooks/session-start.sh`, `hooks/subagent-stop.sh` and
`hooks/worktree-resume.sh` (PreToolUse, matcher `SendMessage`) there, as
`hooks/hooks.json` does (such a repo should not also enable the plugin, or
both would run).

## How it is built

- `src/nixconvoy/`: the Python driver, which performs git, nix and ssh.
- `switch-guard/`: a Rust crate holding the decisions as Sans-I/O state
  machines, with stateright models and property tests: `deploy-plan` (the
  rebase, the union, the drift verdicts, a subagent worktree's pruning
  against the create hook and the sessions), `deploy-switch` (the switch
  protocol), `confirmed-switch` (the host-side helper) and
  `boot-assessment`.
- `tests/switch_test.py`: the switch driven end to end against a fake host,
  the lineage classification, and the git cores on scratch repositories.
- `nix flake check` runs all of it.

## Prior art

nixos-rebuild, deploy-rs and colmena each do things nixconvoy does not,
and nixconvoy exists for the last group, which none of them cover: several checkouts
deploying unmerged work to the same hosts, each safe from the others.

### Targets

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| `boot` and `test` goals | ✓ | ✓ | ✓ |   |
| `dry-activate` goal | ✓ | ✓ | ✓ | ✓ |
| Profiles other than the system (home-manager, user) |   | ✓ |   |   |
| nix-darwin targets |   | ✓ |   |   |
| Non-flake configurations | ✓ |   | ✓ |   |
| Node tags and groups |   |   | ✓ | ✓ |
| Run a command on every node |   |   | ✓ | ✓ |
| Upload secrets outside the store |   |   | ✓ |   |
| Prompt for the sudo password | ✓ | ✓ | ✓ |   |

### Building

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| Build locally | ✓ | ✓ | ✓ | ✓ |
| Build on a designated build host | ✓ |   |   | ✓ |
| Each target builds its own closures | ✓ | ✓ | ✓ |   |

### Performance

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| Parallel switch |   | ✓ | ✓ | ✓ |
| Parallel eval |   |   | ✓ | ✓ |
| Eval skipped when inputs unchanged |   |   |   | ✓ |

### Safety

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| Roll back when the deployer cannot confirm the switch |   | ✓ |   | ✓ |
| Host-side checks decide the confirmation |   |   |   | ✓ |
| A flake check gates the deploy |   | ✓ |   | ✓ |
| Refuses a switch that would restart protected units |   |   |   | ✓ |
| Switches the router (or any host) last |   |   |   | ✓ |
| Commits and pushes only after the switch succeeded |   |   |   | ✓ |
| Boot counting: an unreachable boot falls back to the last proven generation |   |   |   | ✓ |

### Concurrent checkouts

| | nixos-rebuild | deploy-rs | colmena | nixconvoy |
|---|:-:|:-:|:-:|:-:|
| Several checkouts deploy unmerged work to the same hosts |   |   |   | ✓ |
| Refuses to revert another deployer's in-flight work |   |   |   | ✓ |

## License

MIT or Apache-2.0, at your option.

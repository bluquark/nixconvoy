use super::*;
use proptest::prelude::*;

type P = u8;
type F = u8;

fn gen(num: Gen, mtime: i64, path: P) -> Generation<P> {
    Generation { num, mtime, path }
}

#[test]
fn parses_stub_names() {
    let p = Stub::parse;
    assert_eq!(p("nixos-generation-1097-uuu5wh.efi"), Some(Stub { gen: 1097, tries_left: None }));
    assert_eq!(p("nixos-generation-1147-3k2p+2.efi"), Some(Stub { gen: 1147, tries_left: Some(2) }));
    assert_eq!(p("nixos-generation-12-ab+0-2.efi"), Some(Stub { gen: 12, tries_left: Some(0) }));
    assert_eq!(p("nixos-generation-12-ab+10-1.efi"), Some(Stub { gen: 12, tries_left: Some(10) }));
    assert_eq!(p("systemd-bootx64.efi"), None);
    let u = Stub::uncounted;
    assert_eq!(u("nixos-generation-12-ab+1-1.efi").as_deref(), Some("nixos-generation-12-ab.efi"));
    assert_eq!(u("nixos-generation-12-ab+2.efi").as_deref(), Some("nixos-generation-12-ab.efi"));
    assert_eq!(u("nixos-generation-12-ab.efi"), None);
    assert_eq!(u("nixos-generation-12-ab+2"), None);
}

/// The ESP as the install sees it: lzbt keeps exactly the generations it
/// is given (a new stub starts counted) and their files, and refuses a set
/// that does not fit.
#[derive(Clone, Debug)]
struct FakeEsp {
    gens: Vec<Generation<P>>,
    stubs: Vec<Stub>,
    files: Vec<(P, Vec<File<F>>)>,
    size: u64,
    base: u64,
}

impl FakeEsp {
    fn files_for(&self, g: Gen) -> Vec<File<F>> {
        let Some(p) = self.gens.iter().find(|x| x.num == g).map(|x| x.path) else { return vec![] };
        self.files.iter().find(|(x, _)| *x == p).map(|(_, f)| f.clone()).unwrap_or_default()
    }

    fn bytes(&self, gens: &[Gen]) -> u64 {
        let mut seen: Vec<F> = Vec::new();
        let mut t = 0;
        for g in gens {
            for f in self.files_for(*g) {
                if !seen.contains(&f.id) {
                    seen.push(f.id);
                    t += f.size;
                }
            }
        }
        t
    }

    fn used(&self) -> u64 {
        let gens: Vec<Gen> = self.stubs.iter().map(|s| s.gen).collect();
        self.base + self.bytes(&gens)
    }
}

impl Esp for FakeEsp {
    type P = P;
    type F = F;

    fn profile(&mut self) -> Profile<P> {
        Profile::new(self.gens.clone())
    }

    fn delete_generation(&mut self, g: Gen) -> bool {
        self.gens.retain(|x| x.num != g);
        true
    }

    fn stubs(&mut self) -> Vec<(Stub, String)> {
        self.stubs.iter().map(|s| (*s, format!("stub-{}", s.gen))).collect()
    }

    fn files(&mut self, g: Gen) -> Vec<File<F>> {
        self.files_for(g)
    }

    fn install(&mut self, gens: &[Gen]) -> i32 {
        if self.base + self.bytes(gens) > self.size {
            return 1;
        }
        let old = std::mem::take(&mut self.stubs);
        for g in gens {
            let kept = old.iter().find(|s| s.gen == *g).copied();
            self.stubs.push(kept.unwrap_or(Stub { gen: *g, tries_left: Some(2) }));
        }
        0
    }

    fn usage(&mut self) -> (u64, u64) {
        (self.size, self.used())
    }

    fn say(&mut self, _msg: &str) {}

    fn warn(&mut self, _msg: &str) {}
}

#[derive(Clone, Debug)]
struct Case {
    esp: FakeEsp,
    boots: Vec<Boot>,
    target: P,
    booted: Option<P>,
    running: Option<P>,
    previous: Option<P>,
    now: i64,
    reserve: u64,
    bootable: usize,
}

const DAY: i64 = 86400;

type Who = (usize, Option<usize>, Option<usize>, Option<usize>);

fn case() -> impl Strategy<Value = Case> {
    (1usize..14)
        .prop_flat_map(|n| {
            let paths = proptest::collection::vec(0u8..12, n);
            let ages = proptest::collection::vec(0i64..40, n);
            let tries = prop_oneof![Just(None), (0u32..3).prop_map(Some)];
            let stubs = proptest::collection::vec((0usize..n, tries), 0..n + 2);
            let boots = proptest::collection::vec((0i64..45, proptest::option::of(0usize..n + 2)), 0..10);
            let files = (proptest::collection::vec((0u8..6, 0u8..6), 12), proptest::collection::vec(1u64..40, 12));
            let who = (0usize..n, proptest::option::of(0usize..n), proptest::option::of(0usize..n), proptest::option::of(0usize..n));
            (Just(n), paths, ages, stubs, boots, files, who, (0u64..400, 0u64..40, 0u64..20, 7usize..=20))
        })
        .prop_map(|(n, paths, ages, stubs, boots, files, who, (size, base, reserve, bootable))| {
            build(n, paths, ages, stubs, boots, files, who, size, base, reserve, bootable)
        })
}

#[allow(clippy::too_many_arguments)]
fn build(
    n: usize,
    paths: Vec<u8>,
    mut ages: Vec<i64>,
    stubs: Vec<(usize, Option<u32>)>,
    boots: Vec<(i64, Option<usize>)>,
    (files, sizes): (Vec<(u8, u8)>, Vec<u64>),
    (t, b, r, p): Who,
    size: u64,
    base: u64,
    reserve: u64,
    bootable: usize,
) -> Case {
    let now = 50 * DAY;
    // Number order is creation order: newer generations are younger.
    ages.sort_by(|a, b| b.cmp(a));
    let gens: Vec<Generation<P>> = (0..n).map(|i| gen(i as Gen + 1, now - ages[i] * DAY, paths[i])).collect();
    let num = |i: usize| i as Gen + 1;
    let path = |i: usize| gens[i].path;
    let mut seen = Vec::new();
    let stubs = stubs
        .into_iter()
        .filter(|(i, _)| {
            let fresh = !seen.contains(i);
            seen.push(*i);
            fresh
        })
        .map(|(i, tries_left)| Stub { gen: num(i), tries_left })
        .collect();
    let boots = boots.into_iter().map(|(age, g)| Boot { time: now - age * DAY, gen: g.map(num) }).collect();
    let files = files
        .into_iter()
        .enumerate()
        .map(|(p, (k, i))| {
            let f = |id: u8| File { id, size: sizes[id as usize] };
            (p as P, vec![f(k), f(6 + i)])
        })
        .collect();
    Case {
        esp: FakeEsp { gens: gens.clone(), stubs, files, size, base },
        boots,
        target: path(t),
        booted: b.map(path),
        running: r.map(path),
        previous: p.map(path),
        now,
        reserve,
        bootable,
    }
}

const TIERS: [u64; 3] = [1, 7, 30];

fn policy(c: &Case) -> Policy {
    Policy { bootable: c.bootable, tiers: TIERS.to_vec(), reserve: c.reserve }
}

fn fixed_policy() -> Policy {
    Policy { bootable: 15, tiers: TIERS.to_vec(), reserve: 0 }
}

proptest! {
    #![proptest_config(ProptestConfig { cases: 4000, failure_persistence: None, ..ProptestConfig::default() })]

    #[test]
    fn install_properties(c in case()) {
        let mut esp = c.esp.clone();
        let ctx = Ctx { target: c.target, booted: c.booted, running: c.running, previous: c.previous, now: c.now };
        let before = Profile::new(c.esp.gens.clone());
        let stubs0: Vec<Stub> = c.esp.stubs.clone();
        let anchors = anchors(&before, &c.boots, &stubs0, c.now, &TIERS);
        let out = install(&mut esp, &c.boots, &ctx, &policy(&c));
        let after = Profile::new(esp.gens.clone());
        let tgen0 = before.gen_of(Some(&ctx.target));

        // Only never-proven or rejected generations are dropped, never an
        // anchor, the target or the booted one, nor the running or previous
        // one when older than the target.
        for g in &out.dropped {
            prop_assert!(!proven(&c.boots, *g) || rejected(&stubs0, *g), "dropped proven {}", g);
            prop_assert!(!anchors.contains(g), "dropped anchor {}", g);
            let p = before.path(*g).unwrap();
            prop_assert!(*p != ctx.target && Some(p) != ctx.booted.as_ref());
            if Some(p) == ctx.running.as_ref() || Some(p) == ctx.previous.as_ref() {
                prop_assert!(tgen0.is_some_and(|t| *g >= t), "dropped replaced {}", g);
            }
            prop_assert!(stubs0.iter().any(|s| s.gen == *g && s.tries_left.is_some()), "dropped uncounted {}", g);
        }

        // Target, booted, running and previous are always chosen, first.
        let sel = &out.selection;
        let must: Vec<Gen> = [Some(&ctx.target), ctx.booted.as_ref(), ctx.running.as_ref(), ctx.previous.as_ref()]
            .into_iter()
            .filter_map(|p| after.gen_of(p))
            .collect();
        for g in &must {
            prop_assert!(sel.chosen[..sel.must].contains(g), "must {} not chosen: {:?}", g, sel);
        }
        prop_assert!(sel.chosen.len() <= c.bootable);
        for (i, g) in sel.chosen.iter().enumerate() {
            prop_assert!(after.has(*g));
            prop_assert!(!sel.chosen[..i].contains(g));
        }

        // The chosen set fits with the reserve, or nothing is left to shed.
        let on_esp: Vec<F> = esp
            .stubs
            .iter()
            .filter(|s| after.has(s.gen))
            .flat_map(|s| esp.files_for(s.gen))
            .map(|f| f.id)
            .collect();
        let files: Files<F> = sel.chosen.iter().map(|g| (*g, esp.files_for(*g))).collect();
        let total = c.esp.base + esp.bytes(&sel.chosen);
        if total + c.reserve > c.esp.size {
            prop_assert_eq!(shed(sel, &files, &on_esp), None);
            prop_assert!(out.code == 0 || out.shortfall.is_some_and(|b| b > 0), "no shortfall reported: {:?}", out.shortfall);
        } else {
            prop_assert_eq!(out.shortfall, None);
            prop_assert_eq!(out.code, 0);
            let mut stubbed: Vec<Gen> = esp.stubs.iter().map(|s| s.gen).collect();
            stubbed.sort();
            let mut chosen = sel.chosen.clone();
            chosen.sort();
            prop_assert_eq!(stubbed, chosen);
            prop_assert!(esp.used() + c.reserve <= c.esp.size);
        }
    }

    /// The prune keeps exactly the install's bootable set and the newest
    /// generations: what the failover needs survives it, and nothing else.
    #[test]
    fn prune_keeps_the_bootable_set(c in case(), keep_newest in 0usize..6) {
        let profile = Profile::new(c.esp.gens.clone());
        let ctx = Ctx { target: c.target, booted: c.booted, running: c.running, previous: c.previous, now: c.now };
        let pol = policy(&c);
        let anchors = anchors(&profile, &c.boots, &c.esp.stubs, c.now, &pol.tiers);
        let set = choose(&profile, &ctx, &anchors, &c.esp.stubs, pol.bootable).chosen;
        let dropped = prune(&profile, &c.boots, &c.esp.stubs, &ctx, &pol, keep_newest);
        let newest: Vec<Gen> = profile.gens().iter().take(keep_newest).map(|x| x.num).collect();
        let named = [Some(&ctx.target), ctx.booted.as_ref(), ctx.running.as_ref(), ctx.previous.as_ref()];
        for x in profile.gens() {
            let kept = set.contains(&x.num) || newest.contains(&x.num)
                || c.esp.stubs.iter().any(|s| s.gen == x.num) || named.contains(&Some(&x.path));
            prop_assert_eq!(dropped.contains(&x.num), !kept, "generation {}", x.num);
        }
        // The named generations and the anchors always fit: bootable is at least 7.
        for a in &anchors {
            prop_assert!(!dropped.contains(a), "dropped anchor {}", a);
        }
        if let Some(b) = blessed(&c.esp.stubs).into_iter().find(|g| profile.has(*g)) {
            prop_assert!(set.len() == pol.bootable || !dropped.contains(&b), "dropped the newest blessed {}", b);
        }
    }
}

#[test]
fn prune_ignores_age() {
    // The oldest, 1, is the 30-day anchor; 3 is booted, blessed and the
    // other tiers' anchor; 6 is the target.  The youngest, 7, is kept only
    // as one of the newest.
    let now = 100 * DAY;
    let profile = Profile::new((1..=7).map(|n| gen(n, now - (60 - n as i64 * 5) * DAY, n as P)).collect());
    let stubs = [Stub { gen: 1, tries_left: None }, Stub { gen: 3, tries_left: None }, Stub { gen: 6, tries_left: Some(2) }];
    let boots = [Boot { time: now - 40 * DAY, gen: Some(1) }, Boot { time: now - 20 * DAY, gen: Some(3) }];
    let ctx = Ctx { target: 6, booted: Some(3), running: Some(6), previous: None, now };
    let pol = fixed_policy();
    assert_eq!(anchors(&profile, &boots, &stubs, now, &pol.tiers), vec![3, 3, 1]);
    assert_eq!(prune(&profile, &boots, &stubs, &ctx, &pol, 0), vec![7, 5, 4, 2]);
    assert_eq!(prune(&profile, &boots, &stubs, &ctx, &pol, 2), vec![5, 4, 2]);
    assert!(prune(&profile, &boots, &stubs, &ctx, &pol, 7).is_empty());
    // A stub outside the chosen set (a former anchor's, re-stubbed, spent
    // or not) keeps its generation until an install takes the stub off.
    let stubs = [stubs[0], stubs[1], stubs[2], Stub { gen: 4, tries_left: Some(0) }, Stub { gen: 2, tries_left: Some(1) }];
    assert_eq!(prune(&profile, &boots, &stubs, &ctx, &pol, 0), vec![7, 5]);
}

#[test]
fn prune_keeps_every_generation_of_a_named_system() {
    // 1 and 3 are the same system; the profile points at 1 (a rollback by
    // hand), which nix-env refuses to delete, and 3 is its newest duplicate.
    let now = 100 * DAY;
    let profile = Profile::new(vec![gen(1, now - 3 * DAY, 1), gen(2, now - 2 * DAY, 2), gen(3, now - DAY, 1), gen(4, now, 4)]);
    let ctx = Ctx { target: 1, booted: Some(1), running: Some(1), previous: None, now };
    assert_eq!(prune(&profile, &[], &[], &ctx, &fixed_policy(), 0), vec![4, 2]);
}

#[test]
fn falls_back_to_the_generation_active_at_the_cutoff() {
    let now = 100 * DAY;
    let profile = Profile::new(vec![gen(1, now - 40 * DAY, 1), gen(2, now - 10 * DAY, 2), gen(3, now - DAY / 2, 3)]);
    assert_eq!(anchors(&profile, &[], &[], now, &[1, 7, 30]), vec![2, 2, 1]);
    let boots = [Boot { time: now - 8 * DAY, gen: Some(3) }, Boot { time: now - 2 * DAY, gen: Some(2) }];
    // A logged boot wins; a rejected one is skipped.
    assert_eq!(anchor(&profile, &boots, &[], now, 1), Some(2));
    let rej = [Stub { gen: 2, tries_left: Some(0) }];
    assert_eq!(anchor(&profile, &boots, &rej, now, 1), Some(3));
}

#[test]
fn rollback_backs_out_the_newer_running_generation() {
    // Running 3 (never booted, newer than the target 2 being restored):
    // its stub is dropped so systemd-boot does not pick it again.
    let now = 100 * DAY;
    let profile = Profile::new(vec![gen(1, now - 3 * DAY, 1), gen(2, now - 2 * DAY, 2), gen(3, now - DAY, 3)]);
    let stubs = [Stub { gen: 2, tries_left: None }, Stub { gen: 3, tries_left: Some(2) }];
    let boots = [Boot { time: now - 2 * DAY, gen: Some(2) }];
    let ctx = Ctx { target: 2, booted: Some(2), running: Some(3), previous: None, now };
    assert_eq!(drops(&profile, &boots, &stubs, &ctx, &[]), vec![1]);
    // A deploy of 3 keeps the replaced (older) running one.
    let ctx = Ctx { target: 3, booted: Some(1), running: Some(2), previous: Some(2), now };
    let stubs = [Stub { gen: 2, tries_left: Some(1) }, Stub { gen: 3, tries_left: Some(2) }];
    assert!(drops(&profile, &[], &stubs, &ctx, &[]).is_empty());
}

#[test]
fn sheds_the_fill_first_then_the_youngest_anchor() {
    let f = |id, size| File { id, size };
    let files: Files<u8> = vec![
        (9, vec![f(1, 10), f(2, 10)]),
        (8, vec![f(1, 10), f(3, 10)]),
        (5, vec![f(4, 10), f(5, 10)]),
        (4, vec![f(6, 10), f(7, 10)]),
        (3, vec![f(1, 10), f(2, 10)]),
        (2, vec![f(8, 10), f(9, 10)]),
    ];
    let mut sel = Selection { chosen: vec![9, 8, 5, 4, 3, 2], must: 2, fixed: 4 };
    // 2 has unshared files, 3 shares all of 9's.
    assert_eq!(shed(&sel, &files, &[]), Some(5));
    sel.remove(5);
    assert_eq!(shed(&sel, &files, &[]), Some(3));
    sel.remove(3);
    assert_eq!(shed(&sel, &files, &[6, 7]), Some(2));
    sel.remove(2);
    assert_eq!(sel, Selection { chosen: vec![9, 8, 3], must: 2, fixed: 2 });
    assert_eq!(shed(&sel, &files, &[]), None);
}

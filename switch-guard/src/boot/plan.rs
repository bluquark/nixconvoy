//! The bootable set on the ESP: the target, booted, running and previous
//! generations, the anchors - for each tier, the generation booted at the
//! newest boot at least that many days old - then the newest blessed ones
//! (an uncounted stub: proven by a boot) up to `bootable`.  Blessed stubs
//! are about one per nightly boot, so the fill reaches back about two weeks;
//! the last tier reaches past it, and the anchors survive the fit check's
//! shedding, which takes the fill first.  A never-booted generation is never
//! stubbed just to fill: the drop would take it out again at the next
//! deploy, and every stub costs an initrd copy, two hashes and a signature.
//!
//! Pinning anchors to logged boots makes them proven generations that move
//! once a day, not never-booted deploys that a "now minus N days" cutoff
//! would sweep through; until the log reaches back far enough, a tier falls
//! back to the generation active at that cutoff.  An anchor past the fill is
//! reinstalled with a fresh counter; when its tier moves on, the boot log
//! still marks it proven, so only its stub goes.
//!
//! Generic over the store path type `P` and file identity `F`, so the tests
//! can use small integers.
use crate::text;

pub type Gen = u64;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Generation<P> {
    pub num: Gen,
    /// The profile link's mtime, in seconds.
    pub mtime: i64,
    pub path: P,
}

/// The system profile's generations, newest first.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Profile<P> {
    gens: Vec<Generation<P>>,
}

impl<P: Eq> Profile<P> {
    pub fn new(mut gens: Vec<Generation<P>>) -> Self {
        gens.sort_by_key(|g| std::cmp::Reverse(g.num));
        Profile { gens }
    }

    pub fn gens(&self) -> &[Generation<P>] {
        &self.gens
    }

    pub fn path(&self, g: Gen) -> Option<&P> {
        self.gens.iter().find(|x| x.num == g).map(|x| &x.path)
    }

    pub fn has(&self, g: Gen) -> bool {
        self.path(g).is_some()
    }

    /// The newest generation pointing at `p` (after a rollback several can).
    pub fn gen_of(&self, p: Option<&P>) -> Option<Gen> {
        let p = p?;
        self.gens.iter().find(|x| &x.path == p).map(|x| x.num)
    }
}

/// A unified kernel image on the ESP.  `tries_left`: None once blessed
/// (systemd-bless-boot removed the counter), Some(0) once rejected.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Stub {
    pub gen: Gen,
    pub tries_left: Option<u32>,
}

impl Stub {
    /// `nixos-generation-<n>-<hash>[+<left>[-<done>]].efi`
    pub fn parse(name: &str) -> Option<Stub> {
        let rest = name.strip_prefix("nixos-generation-")?.strip_suffix(".efi")?;
        let (num, rest) = rest.split_once('-')?;
        let gen = num.parse().ok()?;
        let tries_left = match rest.split_once('+') {
            None => None,
            Some((_, counter)) => {
                let left = counter.split_once('-').map_or(counter, |(l, _)| l);
                Some(left.parse().ok()?)
            }
        };
        Some(Stub { gen, tries_left })
    }

    /// `name` with its counter taken off: what systemd-bless-boot renames
    /// a counted stub to.  None for a name carrying no counter.
    pub fn uncounted(name: &str) -> Option<String> {
        let (base, _) = name.strip_suffix(".efi")?.split_once('+')?;
        Some(format!("{base}.efi"))
    }
}

/// One line of the boot log: when a boot began, and the generation it booted
/// (None when its toplevel was in no generation).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Boot {
    pub time: i64,
    pub gen: Option<Gen>,
}

pub struct Policy {
    pub bootable: usize,
    pub tiers: Vec<u64>,
    pub reserve: u64,
}

pub struct Ctx<P> {
    /// What the bootloader is being installed for.
    pub target: P,
    pub booted: Option<P>,
    pub running: Option<P>,
    /// The system a confirmed switch is replacing (or replaced): it
    /// installs the bootloader after activating, so `running` is already
    /// the new one.
    pub previous: Option<P>,
    pub now: i64,
}

pub fn rejected(stubs: &[Stub], g: Gen) -> bool {
    stubs.iter().any(|s| s.gen == g && s.tries_left == Some(0))
}

pub fn proven(boots: &[Boot], g: Gen) -> bool {
    boots.iter().any(|b| b.gen == Some(g))
}

/// Generations with an uncounted stub, newest first.
pub fn blessed(stubs: &[Stub]) -> Vec<Gen> {
    let mut v: Vec<Gen> = stubs.iter().filter(|s| s.tries_left.is_none()).map(|s| s.gen).collect();
    v.sort_by(|a, b| b.cmp(a));
    v.dedup();
    v
}

/// The generation of the newest logged boot `days` old or older that still
/// exists and is not rejected - failing that, the newest generation created
/// before that boot (before the cutoff itself with no boot that old),
/// skipping rejected ones.
pub fn anchor<P: Eq>(profile: &Profile<P>, boots: &[Boot], stubs: &[Stub], now: i64, days: u64) -> Option<Gen> {
    let cutoff = now.saturating_sub((days as i64).saturating_mul(86400));
    let mut old: Vec<&Boot> = boots.iter().filter(|b| b.time <= cutoff).collect();
    old.sort_by_key(|b| std::cmp::Reverse((b.time, b.gen)));
    for b in &old {
        if let Some(g) = b.gen {
            if profile.has(g) && !rejected(stubs, g) {
                return Some(g);
            }
        }
    }
    let t = old.first().map(|b| b.time).filter(|t| *t > 0).unwrap_or(cutoff);
    profile.gens().iter().find(|x| x.mtime <= t && !rejected(stubs, x.num)).map(|x| x.num)
}

pub fn anchors<P: Eq>(profile: &Profile<P>, boots: &[Boot], stubs: &[Stub], now: i64, tiers: &[u64]) -> Vec<Gen> {
    tiers.iter().filter_map(|d| anchor(profile, boots, stubs, now, *d)).collect()
}

/// The counted stubs (by index) whose generations go: rejected (+0) or
/// superseded before ever being booted - except the target, the booted one,
/// the running one or the one a confirmed switch is replacing when older
/// than the target (after a rollback it is the newer one being backed out,
/// and keeping it would have systemd-boot pick it again), the anchors, and
/// anything the boot log has seen boot (a re-stubbed former anchor).
/// systemd-boot sorts counted entries newest first and
/// spent ones last, so a failed newest falls to the previous deploy, then
/// to the last proven generation instead of each in-between one.
pub fn drops<P: Eq>(profile: &Profile<P>, boots: &[Boot], stubs: &[Stub], ctx: &Ctx<P>, anchors: &[Gen]) -> Vec<usize> {
    let tgen = profile.gen_of(Some(&ctx.target));
    let mut out: Vec<usize> = Vec::new();
    for (i, s) in stubs.iter().enumerate() {
        if s.tries_left.is_none() || out.iter().any(|&j| stubs[j].gen == s.gen) {
            continue;
        }
        let g = s.gen;
        let Some(path) = profile.path(g) else { continue };
        if *path == ctx.target || Some(path) == ctx.booted.as_ref() {
            continue;
        }
        let replaced = Some(path) == ctx.running.as_ref() || Some(path) == ctx.previous.as_ref();
        if replaced && tgen.is_none_or(|t| g < t) {
            continue;
        }
        if anchors.contains(&g) || (proven(boots, g) && !rejected(stubs, g)) {
            continue;
        }
        out.push(i);
    }
    out
}

/// `chosen[..must]`: target, booted, running, previous - never shed.
/// `chosen[must..fixed]`: the anchors.  The rest: the blessed fill.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Selection {
    pub chosen: Vec<Gen>,
    pub must: usize,
    pub fixed: usize,
}

impl Selection {
    pub fn remove(&mut self, i: usize) -> Gen {
        assert!(i >= self.must, "the target, booted, running and previous generations are never shed");
        if i < self.fixed {
            self.fixed -= 1;
        }
        self.chosen.remove(i)
    }
}

pub fn choose<P: Eq>(profile: &Profile<P>, ctx: &Ctx<P>, anchors: &[Gen], stubs: &[Stub], bootable: usize) -> Selection {
    let mut chosen: Vec<Gen> = Vec::new();
    let add = |g: Option<Gen>, chosen: &mut Vec<Gen>| {
        if let Some(g) = g {
            if profile.has(g) && chosen.len() < bootable && !chosen.contains(&g) {
                chosen.push(g);
            }
        }
    };
    add(profile.gen_of(Some(&ctx.target)), &mut chosen);
    add(profile.gen_of(ctx.booted.as_ref()), &mut chosen);
    add(profile.gen_of(ctx.running.as_ref()), &mut chosen);
    add(profile.gen_of(ctx.previous.as_ref()), &mut chosen);
    let must = chosen.len();
    for a in anchors {
        add(Some(*a), &mut chosen);
    }
    let fixed = chosen.len();
    for b in blessed(stubs) {
        add(Some(b), &mut chosen);
    }
    Selection { chosen, must, fixed }
}

/// A kernel or initrd as installed on the ESP: content-addressed there, so
/// generations sharing one share its copy.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct File<F> {
    pub id: F,
    pub size: u64,
}

pub type Files<F> = Vec<(Gen, Vec<File<F>>)>;

fn files_of<F>(files: &Files<F>, g: Gen) -> &[File<F>] {
    files.iter().find(|(x, _)| *x == g).map_or(&[], |(_, f)| f.as_slice())
}

/// Bytes `gens` would add to the ESP: their files not already there.
pub fn new_bytes<F: Eq>(gens: &[Gen], files: &Files<F>, on_esp: &[F]) -> u64 {
    let mut seen: Vec<&F> = Vec::new();
    let mut total = 0u64;
    for g in gens {
        for f in files_of(files, *g) {
            if !on_esp.contains(&f.id) && !seen.contains(&&f.id) {
                seen.push(&f.id);
                total += f.size;
            }
        }
    }
    total
}

fn unshared<F: Eq>(sel: &Selection, i: usize, files: &Files<F>) -> bool {
    let g = sel.chosen[i];
    files_of(files, g).iter().any(|f| !sel.chosen.iter().any(|o| *o != g && files_of(files, *o).iter().any(|x| x.id == f.id)))
}

/// What to leave out when the new files do not fit: the oldest fill entry
/// with a kernel or initrd nothing else in the set shares, then with the
/// fill exhausted the youngest anchor needing new files.  Never the target,
/// booted, running or previous generation.
pub fn shed<F: Eq>(sel: &Selection, files: &Files<F>, on_esp: &[F]) -> Option<usize> {
    (sel.fixed..sel.chosen.len())
        .rev()
        .find(|&i| unshared(sel, i, files))
        .or_else(|| (sel.must..sel.fixed).rev().find(|&i| files_of(files, sel.chosen[i]).iter().any(|f| !on_esp.contains(&f.id))))
}

pub fn fits(size: u64, used: u64, new: u64, reserve: u64) -> bool {
    used.saturating_add(new).saturating_add(reserve) <= size
}

/// The prune's victims: every generation outside the bootable set an
/// install for `ctx` would choose (before the fit check's shedding), not
/// stubbed on the ESP (a stub left from an earlier install, a former
/// anchor's say, must keep booting until the next install takes it off),
/// not pointing at a system `ctx` names (nix-env refuses to delete the
/// current generation, an older duplicate included) and not among the
/// newest `keep_newest`, which a rollback by hand reaches for whether or
/// not they ever booted.
pub fn prune<P: Eq>(profile: &Profile<P>, boots: &[Boot], stubs: &[Stub], ctx: &Ctx<P>, policy: &Policy, keep_newest: usize) -> Vec<Gen> {
    let anchors = anchors(profile, boots, stubs, ctx.now, &policy.tiers);
    let set = choose(profile, ctx, &anchors, stubs, policy.bootable).chosen;
    let named = |p: &P| *p == ctx.target || [&ctx.booted, &ctx.running, &ctx.previous].iter().any(|x| x.as_ref() == Some(p));
    profile
        .gens()
        .iter()
        .skip(keep_newest)
        .filter(|x| !set.contains(&x.num) && !stubs.iter().any(|s| s.gen == x.num) && !named(&x.path))
        .map(|x| x.num)
        .collect()
}

/// The install's effects: the profile, the ESP and lanzaboote.
pub trait Esp {
    type P: Clone + Eq;
    type F: Clone + Eq;
    fn profile(&mut self) -> Profile<Self::P>;
    fn delete_generation(&mut self, g: Gen) -> bool;
    /// The stubs now on the ESP, with their file names.
    fn stubs(&mut self) -> Vec<(Stub, String)>;
    fn files(&mut self, g: Gen) -> Vec<File<Self::F>>;
    /// lzbt over these generations: writes what they lack, collects the
    /// rest.  Its exit code.
    fn install(&mut self, gens: &[Gen]) -> i32;
    /// (size, used) in bytes.
    fn usage(&mut self) -> (u64, u64);
    fn say(&mut self, msg: &str);
    fn warn(&mut self, msg: &str);
}

pub struct Installed {
    pub code: i32,
    pub dropped: Vec<Gen>,
    pub selection: Selection,
    /// Bytes the never-shed part of the set lacks on the ESP, when even it
    /// does not fit.
    pub shortfall: Option<u64>,
}

/// Runs in place of lanzaboote's installer.  lzbt writes a generation's
/// kernel and initrd before collecting what left the set, so it first runs
/// on the part of the set already installed (nothing to write, the rest
/// collected) and the fit check reads the space free after that; a kernel or
/// initrd is only new when no stubbed generation shares it.  While the new
/// files do not fit, `shed` picks what to leave out, and lzbt re-runs so its
/// files are collected before measuring again.
pub fn install<E: Esp>(esp: &mut E, boots: &[Boot], ctx: &Ctx<E::P>, policy: &Policy) -> Installed {
    let profile = esp.profile();
    let stub_files = esp.stubs();
    let stubs: Vec<Stub> = stub_files.iter().map(|(s, _)| *s).collect();
    let anchors = anchors(&profile, boots, &stubs, ctx.now, &policy.tiers);
    let mut dropped = Vec::new();
    for i in drops(&profile, boots, &stubs, ctx, &anchors) {
        let g = stubs[i].gen;
        esp.say(&format!(text!(boot_dropping), g = g, stub = stub_files[i].1));
        if esp.delete_generation(g) {
            dropped.push(g);
        } else {
            esp.warn(&format!(text!(boot_drop_failed), g = g));
        }
    }
    let profile = esp.profile();
    let mut sel = choose(&profile, ctx, &anchors, &stubs, policy.bootable);
    let mut files: Files<E::F> = Vec::new();
    for g in &sel.chosen {
        files.push((*g, esp.files(*g)));
    }
    let mut shortfall = None;
    loop {
        let stubbed: Vec<Gen> = esp.stubs().iter().map(|(s, _)| s.gen).collect();
        let installed: Vec<Gen> = sel.chosen.iter().copied().filter(|g| stubbed.contains(g)).collect();
        if !installed.is_empty() {
            esp.install(&installed);
        }
        let mut on_esp: Vec<E::F> = Vec::new();
        for (s, _) in esp.stubs() {
            if !profile.has(s.gen) {
                continue;
            }
            if !files.iter().any(|(g, _)| *g == s.gen) {
                let f = esp.files(s.gen);
                files.push((s.gen, f));
            }
            on_esp.extend(files_of(&files, s.gen).iter().map(|f| f.id.clone()));
        }
        let (size, used) = esp.usage();
        let new = new_bytes(&sel.chosen, &files, &on_esp);
        if fits(size, used, new, policy.reserve) {
            break;
        }
        let Some(i) = shed(&sel, &files, &on_esp) else {
            shortfall = Some(used.saturating_add(new).saturating_add(policy.reserve).saturating_sub(size));
            break;
        };
        let g = sel.remove(i);
        esp.say(&format!(text!(boot_not_fit), g = g));
    }
    let list = |v: &[Gen]| v.iter().map(Gen::to_string).collect::<Vec<_>>().join(" ");
    esp.say(&format!(text!(boot_installing), chosen = list(&sel.chosen), anchors = list(&anchors)));
    let code = esp.install(&sel.chosen);
    // Measured before the stubs left out are collected, so only a failed
    // install proves it.
    let shortfall = shortfall.filter(|_| code != 0);
    Installed { code, dropped, selection: sel, shortfall }
}

#[cfg(test)]
mod tests;

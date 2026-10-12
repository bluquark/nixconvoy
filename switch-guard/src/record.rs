//! The armed switch's state: one file, replaced by rename under the helper's
//! lock, read by the helper and by boot-assessment's planner (which spares
//! `previous`).
use std::fmt;
use std::io::Write;
use std::path::Path;

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub enum Phase<S> {
    /// The timer is (or was meant to be) pending; activations are admitted.
    Armed,
    /// The profile and bootloader entry name `.0`.
    Confirmed(S),
    /// The target demonstrably ran `previous` again; nothing more is admitted.
    RolledBack,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Record<S> {
    /// What ran when the switch was armed.
    pub previous: S,
    pub phase: Phase<S>,
}

const MAGIC: &str = "confirmed-switch 1";
pub const FILE: &str = "state";

impl<S: fmt::Display> Record<S> {
    pub fn render(&self) -> String {
        let phase = match &self.phase {
            Phase::Armed => "armed".to_string(),
            Phase::RolledBack => "rolled-back".to_string(),
            Phase::Confirmed(t) => format!("confirmed\nconfirmed {t}"),
        };
        format!("{MAGIC}\nprevious {}\nphase {phase}\n", self.previous)
    }
}

impl Record<String> {
    pub fn parse(text: &str) -> Result<Self, String> {
        let mut lines = text.lines();
        if lines.next() != Some(MAGIC) {
            return Err("not a confirmed-switch state file".into());
        }
        let (mut previous, mut phase, mut confirmed) = (None, None, None);
        for line in lines {
            match line.split_once(' ') {
                Some(("previous", v)) => previous = Some(v.to_string()),
                Some(("phase", v)) => phase = Some(v.to_string()),
                Some(("confirmed", v)) => confirmed = Some(v.to_string()),
                _ => return Err(format!("unexpected line {line:?}")),
            }
        }
        let previous = previous.ok_or("no previous")?;
        let phase = match (phase.as_deref(), confirmed) {
            (Some("armed"), None) => Phase::Armed,
            (Some("rolled-back"), None) => Phase::RolledBack,
            (Some("confirmed"), Some(t)) => Phase::Confirmed(t),
            _ => return Err("bad phase".into()),
        };
        Ok(Record { previous, phase })
    }

    /// None when there is no state file (never armed since boot).
    pub fn load(dir: &Path) -> Result<Option<Self>, String> {
        let path = dir.join(FILE);
        match std::fs::read_to_string(&path) {
            Ok(text) => Record::parse(&text).map(Some).map_err(|e| format!("{}: {e}", path.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(format!("{}: {e}", path.display())),
        }
    }

    /// A rename, so a reader (or a helper killed mid-write) sees the old
    /// record or the new one, never a torn one.
    pub fn store(dir: &Path, rec: Option<&Self>) -> std::io::Result<()> {
        let path = dir.join(FILE);
        let Some(rec) = rec else {
            return match std::fs::remove_file(&path) {
                Err(e) if e.kind() != std::io::ErrorKind::NotFound => Err(e),
                _ => Ok(()),
            };
        };
        let tmp = dir.join(format!("{FILE}.tmp"));
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(rec.render().as_bytes())?;
        f.sync_all()?;
        std::fs::rename(&tmp, &path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips() {
        for phase in [Phase::Armed, Phase::RolledBack, Phase::Confirmed("/nix/store/b".to_string())] {
            let r = Record { previous: "/nix/store/a".to_string(), phase };
            assert_eq!(Record::parse(&r.render()).unwrap(), r);
        }
        assert!(Record::parse("previous x\n").is_err());
        assert!(Record::parse("confirmed-switch 1\nprevious a\nphase confirmed\n").is_err());
    }

    #[test]
    fn stores_atomically() {
        let dir = std::env::temp_dir().join(format!("switch-guard-test-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert_eq!(Record::load(&dir).unwrap(), None);
        let r = Record { previous: "/p".to_string(), phase: Phase::Armed };
        Record::store(&dir, Some(&r)).unwrap();
        assert_eq!(Record::load(&dir).unwrap(), Some(r));
        assert!(!dir.join("state.tmp").exists());
        Record::store(&dir, None).unwrap();
        assert_eq!(Record::load(&dir).unwrap(), None);
        std::fs::remove_dir_all(&dir).unwrap();
    }
}

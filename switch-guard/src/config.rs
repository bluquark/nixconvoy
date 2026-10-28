//! The `key = value` file a module renders, so per-target settings and tool
//! paths reach the binary without a wrapper environment.
use crate::text;
use std::collections::BTreeMap;
use std::path::Path;

#[derive(Debug, Default)]
pub struct Config {
    map: BTreeMap<String, String>,
}

impl Config {
    pub fn parse(text: &str) -> Result<Config, String> {
        let mut map = BTreeMap::new();
        for (n, line) in text.lines().enumerate() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let (k, v) = line.split_once('=').ok_or_else(|| format!(text!(config_no_equals), n = n + 1))?;
            map.insert(k.trim().to_string(), v.trim().to_string());
        }
        Ok(Config { map })
    }

    pub fn load(path: &Path) -> Result<Config, String> {
        let text = std::fs::read_to_string(path).map_err(|e| format!(text!(config_cannot_read), path = path.display(), e = e))?;
        Config::parse(&text)
    }

    pub fn str(&self, key: &str) -> Result<&str, String> {
        self.map.get(key).map(String::as_str).ok_or_else(|| format!(text!(config_missing), key = key))
    }

    pub fn opt(&self, key: &str) -> Option<&str> {
        self.map.get(key).map(String::as_str).filter(|v| !v.is_empty())
    }

    pub fn num<T: std::str::FromStr>(&self, key: &str) -> Result<T, String> {
        self.str(key)?.parse().map_err(|_| format!(text!(config_not_a_number), key = key))
    }

    pub fn nums<T: std::str::FromStr>(&self, key: &str) -> Result<Vec<T>, String> {
        self.str(key)?
            .split_whitespace()
            .map(|w| w.parse().map_err(|_| format!(text!(config_word_not_a_number), key = key, w = w)))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses() {
        let c = Config::parse("# c\na = 1\n b=x y \n\nempty =\n").unwrap();
        assert_eq!(c.num::<u32>("a").unwrap(), 1);
        assert_eq!(c.str("b").unwrap(), "x y");
        assert_eq!(c.opt("empty"), None);
        assert!(c.str("missing").is_err());
        assert!(Config::parse("novalue").is_err());
    }
}

//! The caps, keyed by full name. Each reads its own args with the
//! manifest's defaults: the hub passes args through unchecked.

use serde_json::{Map, Value};

pub mod file;
pub mod shell;

pub type Reply = Result<Value, String>;

pub struct Args<'a>(pub &'a Map<String, Value>);

impl Args<'_> {
    fn get(&self, k: &str) -> Option<&Value> {
        self.0.get(k).filter(|v| !v.is_null())
    }
    pub fn str(&self, k: &str) -> Result<Option<String>, String> {
        match self.get(k) {
            None => Ok(None),
            Some(Value::String(s)) => Ok(Some(s.clone())),
            Some(_) => Err(format!("{k} must be a string")),
        }
    }
    pub fn need(&self, k: &str) -> Result<String, String> {
        self.str(k)?.ok_or(format!("{k} is required"))
    }
    pub fn bool(&self, k: &str, default: bool) -> Result<bool, String> {
        match self.get(k) {
            None => Ok(default),
            Some(Value::Bool(b)) => Ok(*b),
            Some(_) => Err(format!("{k} must be true or false")),
        }
    }
    pub fn float(&self, k: &str, default: f64) -> Result<f64, String> {
        match self.get(k) {
            None => Ok(default),
            Some(v) => v.as_f64().ok_or(format!("{k} must be a number")),
        }
    }
    pub fn int(&self, k: &str, default: u64) -> Result<u64, String> {
        match self.get(k) {
            None => Ok(default),
            Some(v) => v.as_u64().ok_or(format!("{k} must be a whole number")),
        }
    }
    pub fn strings(&self, k: &str) -> Result<Option<Vec<String>>, String> {
        match self.get(k) {
            None => Ok(None),
            Some(Value::Array(a)) => a
                .iter()
                .map(|v| v.as_str().map(String::from).ok_or(format!("{k} must be a list of strings")))
                .collect::<Result<_, _>>()
                .map(Some),
            Some(_) => Err(format!("{k} must be a list of strings")),
        }
    }
    /// A map of names to values, each as text (numbers and booleans too).
    pub fn env(&self, k: &str) -> Result<Vec<(String, String)>, String> {
        match self.get(k) {
            None => Ok(vec![]),
            Some(Value::Object(m)) => Ok(m
                .iter()
                .map(|(n, v)| (n.clone(), v.as_str().map(String::from).unwrap_or_else(|| v.to_string())))
                .collect()),
            Some(_) => Err(format!("{k} must be a map")),
        }
    }
}

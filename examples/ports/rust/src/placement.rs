//! Placement expressions (spec "Placement"): a small Python-syntax predicate
//! language over node facts, parsed without eval.

use serde_json::{Map, Value};

pub struct NodeFacts {
    pub roles: Vec<String>,
    pub hw: Map<String, Value>,
}

#[derive(Debug)]
enum Node {
    Const(Value),
    Name(String),
    Seq(Vec<Node>),
    Not(Box<Node>),
    And(Vec<Node>),
    Or(Vec<Node>),
    Cmp(Box<Node>, Vec<(String, Node)>),
    Has(String, Vec<Node>, Vec<(String, Node)>),
}

#[derive(Debug, Clone, PartialEq)]
enum Tok {
    Name(String),
    Str(String),
    Num(String),
    Op(String),
}

type R<T> = Result<T, String>;

const KEYWORDS: &[&str] = &["and", "or", "not", "in", "is", "if", "else", "for", "lambda", "import", "def",
                            "return", "yield", "await", "async", "del", "pass"];

fn tokenize(src: &str) -> R<Vec<Tok>> {
    let c: Vec<char> = src.chars().collect();
    let (mut i, mut out) = (0, vec![]);
    while i < c.len() {
        let ch = c[i];
        if ch == ' ' || ch == '\t' {
            i += 1;
        } else if ch.is_ascii_alphabetic() || ch == '_' {
            let j = (i..c.len()).find(|&j| !(c[j].is_ascii_alphanumeric() || c[j] == '_')).unwrap_or(c.len());
            out.push(Tok::Name(c[i..j].iter().collect()));
            i = j;
        } else if ch.is_ascii_digit() || (ch == '.' && c.get(i + 1).is_some_and(|d| d.is_ascii_digit())) {
            let mut j = i;
            while j < c.len() && (c[j].is_ascii_digit() || c[j] == '.') {
                j += 1;
            }
            if j < c.len() && (c[j] == 'e' || c[j] == 'E') {
                j += 1;
                if j < c.len() && (c[j] == '+' || c[j] == '-') {
                    j += 1;
                }
                while j < c.len() && c[j].is_ascii_digit() {
                    j += 1;
                }
            }
            out.push(Tok::Num(c[i..j].iter().collect()));
            i = j;
        } else if ch == '\'' || ch == '"' {
            let (mut j, mut s) = (i + 1, String::new());
            while j < c.len() && c[j] != ch {
                if c[j] == '\\' && j + 1 < c.len() {
                    s.push(match c[j + 1] { 'n' => '\n', 't' => '\t', o => o });
                    j += 2;
                } else {
                    s.push(c[j]);
                    j += 1;
                }
            }
            if j >= c.len() {
                return Err("unterminated string".into());
            }
            out.push(Tok::Str(s));
            i = j + 1;
        } else {
            let two: String = c[i..(i + 2).min(c.len())].iter().collect();
            if ["==", "!=", "<=", ">="].contains(&two.as_str()) {
                out.push(Tok::Op(two));
                i += 2;
            } else if "()[],=<>".contains(ch) {
                out.push(Tok::Op(ch.to_string()));
                i += 1;
            } else {
                return Err(format!("unexpected {:?}", ch));
            }
        }
    }
    Ok(out)
}

struct Parser {
    t: Vec<Tok>,
    i: usize,
}

impl Parser {
    fn is(&self, v: &str) -> bool {
        matches!(self.t.get(self.i), Some(Tok::Op(x)) | Some(Tok::Name(x)) if x == v)
    }
    fn eat(&mut self, v: &str) -> bool {
        let hit = self.is(v);
        if hit {
            self.i += 1;
        }
        hit
    }
    fn need(&mut self, v: &str) -> R<()> {
        if self.eat(v) { Ok(()) } else { Err(format!("expected {}", v)) }
    }
    fn expr(&mut self) -> R<Node> {
        let mut es = vec![self.and()?];
        while self.eat("or") {
            es.push(self.and()?);
        }
        Ok(if es.len() > 1 { Node::Or(es) } else { es.pop().unwrap() })
    }
    fn and(&mut self) -> R<Node> {
        let mut es = vec![self.not()?];
        while self.eat("and") {
            es.push(self.not()?);
        }
        Ok(if es.len() > 1 { Node::And(es) } else { es.pop().unwrap() })
    }
    fn not(&mut self) -> R<Node> {
        if self.eat("not") { Ok(Node::Not(Box::new(self.not()?))) } else { self.cmp() }
    }
    fn cmp(&mut self) -> R<Node> {
        let first = self.atom()?;
        let mut rest = vec![];
        loop {
            let op = match self.t.get(self.i) {
                Some(Tok::Op(o)) if ["==", "!=", "<", "<=", ">", ">="].contains(&o.as_str()) => o.clone(),
                Some(Tok::Name(n)) if n == "in" => "in".to_string(),
                Some(Tok::Name(n)) if n == "not" && self.t.get(self.i + 1) == Some(&Tok::Name("in".into())) => {
                    self.i += 1;
                    "not in".to_string()
                }
                Some(Tok::Name(n)) if n == "is" => return Err("'is' is not allowed".into()),
                _ => break,
            };
            self.i += 1;
            rest.push((op, self.atom()?));
        }
        Ok(if rest.is_empty() { first } else { Node::Cmp(Box::new(first), rest) })
    }
    fn items(&mut self, close: &str) -> R<Vec<Node>> {
        let mut v = vec![];
        while !self.eat(close) {
            v.push(self.expr()?);
            if !self.eat(",") {
                self.need(close)?;
                break;
            }
        }
        Ok(v)
    }
    fn atom(&mut self) -> R<Node> {
        let t = self.t.get(self.i).cloned().ok_or("unexpected end")?;
        self.i += 1;
        match t {
            Tok::Str(s) => Ok(Node::Const(Value::String(s))),
            Tok::Num(n) => {
                if let Ok(i) = n.parse::<i64>() {
                    return Ok(Node::Const(Value::from(i)));
                }
                n.parse::<f64>().map(|f| Node::Const(Value::from(f))).map_err(|_| format!("bad number {}", n))
            }
            Tok::Op(o) if o == "(" => {
                if self.eat(")") {
                    return Ok(Node::Seq(vec![]));
                }
                let first = self.expr()?;
                if self.eat(")") {
                    return Ok(first);
                }
                self.need(",")?;
                let mut v = vec![first];
                v.extend(self.items(")")?);
                Ok(Node::Seq(v))
            }
            Tok::Op(o) if o == "[" => Ok(Node::Seq(self.items("]")?)),
            Tok::Name(n) => match n.as_str() {
                "True" => Ok(Node::Const(Value::Bool(true))),
                "False" => Ok(Node::Const(Value::Bool(false))),
                "None" => Ok(Node::Const(Value::Null)),
                k if KEYWORDS.contains(&k) => Err(format!("{} is not allowed", k)),
                _ if self.eat("(") => {
                    if n != "has" {
                        return Err("only has(...) calls are allowed".into());
                    }
                    let Some(Tok::Str(fact)) = self.t.get(self.i).cloned() else {
                        return Err("has() needs a fact name first".into());
                    };
                    self.i += 1;
                    let (mut conds, mut kw) = (vec![], vec![]);
                    while self.eat(",") {
                        if self.is(")") {
                            break;
                        }
                        match (self.t.get(self.i).cloned(), self.t.get(self.i + 1)) {
                            (Some(Tok::Name(k)), Some(Tok::Op(eq))) if eq == "=" => {
                                self.i += 2;
                                kw.push((k, self.expr()?));
                            }
                            _ => {
                                if !kw.is_empty() {
                                    return Err("positional argument after keyword".into());
                                }
                                conds.push(self.expr()?);
                            }
                        }
                    }
                    self.need(")")?;
                    Ok(Node::Has(fact, conds, kw))
                }
                _ => Ok(Node::Name(n)),
            },
            Tok::Op(o) => Err(format!("unexpected {}", o)),
        }
    }
}

fn compile(expr: &str) -> R<Node> {
    if expr.trim().is_empty() {
        return Err("empty placement".into());
    }
    let mut p = Parser { t: tokenize(expr.trim())?, i: 0 };
    let n = p.expr()?;
    if p.i != p.t.len() {
        return Err("trailing input".into());
    }
    Ok(n)
}

/// Parse and validate an expression (errors at declaration time).
pub fn compile_placement(expr: &str) -> Result<(), String> {
    compile(expr).map(|_| ())
}

// Python-like semantics: truthiness, ==, ordering errors on mixed types.
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}
fn num(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}
fn eq(a: &Value, b: &Value) -> bool {
    match (num(a), num(b)) {
        (Some(x), Some(y)) => x == y,
        _ => match (a, b) {
            (Value::Array(x), Value::Array(y)) => x.len() == y.len() && x.iter().zip(y).all(|(p, q)| eq(p, q)),
            (Value::Object(x), Value::Object(y)) => {
                x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).is_some_and(|w| eq(v, w)))
            }
            _ => a == b,
        },
    }
}
fn order(a: &Value, b: &Value) -> R<std::cmp::Ordering> {
    if let (Some(x), Some(y)) = (num(a), num(b)) {
        return x.partial_cmp(&y).ok_or_else(|| "nan".into());
    }
    match (a, b) {
        (Value::String(x), Value::String(y)) => Ok(x.cmp(y)),
        _ => Err("unorderable types".into()),
    }
}
fn contains(item: &Value, bx: &Value) -> R<bool> {
    match bx {
        Value::String(s) => item.as_str().map(|i| s.contains(i)).ok_or_else(|| "'in <string>' needs a string".into()),
        Value::Array(a) => Ok(a.iter().any(|v| eq(item, v))),
        Value::Object(o) => Ok(item.as_str().is_some_and(|k| o.contains_key(k))),
        _ => Err("not iterable".into()),
    }
}
fn cmp(op: &str, a: &Value, b: &Value) -> R<bool> {
    use std::cmp::Ordering::*;
    let some = !a.is_null() && !b.is_null();
    Ok(match op {
        "==" => eq(a, b),
        "!=" => !eq(a, b),
        "<" => some && order(a, b)? == Less,
        "<=" => some && order(a, b)? != Greater,
        ">" => some && order(a, b)? == Greater,
        ">=" => some && order(a, b)? != Less,
        "in" => !b.is_null() && contains(a, b)?,
        "not in" => !b.is_null() && !contains(a, b)?,
        _ => return Err(format!("bad op {}", op)),
    })
}

fn ev(n: &Node, f: &NodeFacts, scope: Option<&Map<String, Value>>) -> R<Value> {
    Ok(match n {
        Node::Const(v) => v.clone(),
        Node::Seq(items) => Value::Array(items.iter().map(|e| ev(e, f, scope)).collect::<R<_>>()?),
        Node::Name(id) => match scope {
            Some(s) => s.get(id).cloned().unwrap_or(Value::Null),
            None if id == "any" || id == "anywhere" => Value::Bool(true),
            None if id == "is_embedded" => Value::Bool(f.hw.get("embedded").is_some_and(truthy)),
            None if id.starts_with("is_") => Value::Bool(f.roles.iter().any(|r| r == id)),
            None => f.hw.get(id).cloned().unwrap_or(Value::Null),
        },
        Node::Not(e) => Value::Bool(!truthy(&ev(e, f, scope)?)),
        Node::And(es) => {
            for e in es {
                if !truthy(&ev(e, f, scope)?) {
                    return Ok(Value::Bool(false));
                }
            }
            Value::Bool(true)
        }
        Node::Or(es) => {
            for e in es {
                if truthy(&ev(e, f, scope)?) {
                    return Ok(Value::Bool(true));
                }
            }
            Value::Bool(false)
        }
        Node::Cmp(first, rest) => {
            let mut left = ev(first, f, scope)?;
            for (op, e) in rest {
                let right = ev(e, f, scope)?;
                if !cmp(op, &left, &right)? {
                    return Ok(Value::Bool(false));
                }
                left = right;
            }
            Value::Bool(true)
        }
        Node::Has(fact, conds, kw) => {
            let want: Vec<(String, Value)> =
                kw.iter().map(|(k, e)| Ok((k.clone(), ev(e, f, None)?))).collect::<R<_>>()?;
            let v = f.hw.get(fact).cloned().unwrap_or(Value::Null);
            let items = match v {
                Value::Array(a) => a,
                other => vec![other],
            };
            let empty = Map::new();
            for item in &items {
                if !truthy(item) {
                    continue;
                }
                if conds.is_empty() && want.is_empty() {
                    return Ok(Value::Bool(true));
                }
                let d = item.as_object().unwrap_or(&empty);
                let mut ok = want.iter().all(|(k, w)| eq(d.get(k).unwrap_or(&Value::Null), w));
                if ok {
                    for c in conds {
                        if !truthy(&ev(c, f, Some(d))?) {
                            ok = false;
                            break;
                        }
                    }
                }
                if ok {
                    return Ok(Value::Bool(true));
                }
            }
            Value::Bool(false)
        }
    })
}

/// Evaluate a predicate; invalid expressions and evaluation errors are false.
pub fn evaluate_placement(expr: Option<&str>, facts: &NodeFacts) -> bool {
    let Some(expr) = expr else { return true };
    compile(expr).and_then(|n| ev(&n, facts, None)).map(|v| truthy(&v)).unwrap_or(false)
}

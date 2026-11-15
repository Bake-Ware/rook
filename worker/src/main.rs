//! `rook-worker --config <worker.yaml>`: the worker on the band that
//! config's enrollment joined.
//! `rook-worker --config <worker.yaml> enroll <hub address or QR text> [--code CODE] [--name NAME] [--insecure] [--force]`:
//! join a hub (with a one-time code, or by asking and waiting for a Join tap).

use std::path::PathBuf;

const USAGE: &str =
    "usage: rook-worker --config <worker.yaml> [enroll <hub> [--code CODE] [--name NAME] [--insecure] [--force]]";

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let mut config = None;
    let (mut rest, mut i) = (vec![], 0);
    while i < args.len() {
        if args[i] == "--config" {
            config = args.get(i + 1).map(PathBuf::from);
            i += 2;
        } else {
            rest.push(args[i].clone());
            i += 1;
        }
    }
    let Some(config) = config else { fail(USAGE, 2) };
    if rest.first().map(String::as_str) == Some("enroll") {
        std::process::exit(enroll(&config, &rest[1..]));
    }
    if !rest.is_empty() {
        fail(USAGE, 2);
    }
    let cfg = rook_worker::config::load(&config).unwrap_or_else(|e| fail(&format!("rook-worker: {e}"), 2));
    rook_worker::core::select(&cfg.data); // the core state.json picks: it may exec another file
    let rt = tokio::runtime::Runtime::new().expect("a tokio runtime");
    let out = rt.block_on(async {
        let worker = rook_worker::Worker::start(cfg, false).await?;
        worker.run().await
    });
    if let Err(e) = out {
        fail(&format!("rook-worker: {e}"), 1);
    }
}

fn fail(msg: &str, code: i32) -> ! {
    eprintln!("{msg}");
    std::process::exit(code)
}

fn enroll(config: &std::path::Path, args: &[String]) -> i32 {
    let (mut hub, mut code, mut name, mut insecure, mut force) = (None, None, None, false, false);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        match a.as_str() {
            "--code" => code = it.next().cloned(),
            "--name" => name = it.next().cloned(),
            "--insecure" => insecure = true,
            "--force" => force = true,
            _ if hub.is_none() && !a.starts_with("--") => hub = Some(a.clone()),
            _ => fail(USAGE, 2),
        }
    }
    let Some(hub) = hub else {
        fail("enroll: give the hub's address (or the text of its QR)", 2)
    };
    let rt = tokio::runtime::Runtime::new().expect("a tokio runtime");
    let say = |s: &str| println!("{s}");
    match rt.block_on(rook_worker::enroll::enroll(
        config,
        &hub,
        code.as_deref(),
        name.as_deref(),
        insecure,
        force,
        &say,
    )) {
        Ok(e) => {
            println!(
                "Enrolled {} ({}) with hub {}, owned by {}.\nStart it:  rook-worker --config {}",
                e.name,
                e.worker_id,
                e.hub,
                e.owner.as_str().unwrap_or("?"),
                e.config.display()
            );
            0
        }
        Err(e) => {
            eprintln!("enroll: {e}");
            1
        }
    }
}

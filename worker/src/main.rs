//! `rook-worker --config <worker.yaml>`: the worker on the band that
//! config's enrollment joined.

use std::path::PathBuf;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let config = match args.iter().position(|a| a == "--config").and_then(|i| args.get(i + 1)) {
        Some(path) => PathBuf::from(path),
        None => {
            eprintln!("usage: rook-worker --config <worker.yaml>");
            std::process::exit(2);
        }
    };
    let cfg = rook_worker::config::load(&config).unwrap_or_else(|e| {
        eprintln!("rook-worker: {e}");
        std::process::exit(2);
    });
    rook_worker::core::select(&cfg.data); // the core state.json picks: it may exec another file
    let rt = tokio::runtime::Runtime::new().expect("a tokio runtime");
    let out = rt.block_on(async {
        let worker = rook_worker::Worker::start(cfg).await?;
        worker.run().await
    });
    if let Err(e) = out {
        eprintln!("rook-worker: {e}");
        std::process::exit(1);
    }
}

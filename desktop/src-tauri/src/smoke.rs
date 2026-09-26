//! `plexora-desktop --smoke-test`: prove an installed app can start its
//! server, without opening a window or initialising a display.
//!
//! Used by `scripts/release.py validate` on every artifact. Exit codes:
//! 0 ok, 3 runtime not found, 4 no ready line in time, 5 health check failed,
//! 6 the server did not exit when asked.

use std::time::{Duration, Instant};

use crate::server::{self, Server, StartError};

pub fn run() -> i32 {
    let started = Instant::now();
    let launch = match server::find_launch(None) {
        Ok(launch) => launch,
        Err(error) => {
            eprintln!("{error}");
            return 3;
        }
    };
    println!("runtime: {}", launch.python().display());
    let shared = Server::default();
    let ready = match shared.start(&launch, None, &|message| println!("{message}")) {
        Ok(ready) => ready,
        Err(error) => {
            eprintln!("{error}\n{}", shared.tail(40));
            return match error {
                StartError::NotFound(_) => 3,
                _ => 4,
            };
        }
    };
    let boot = started.elapsed();
    let health = server::http_get(
        &ready.origin,
        &format!("/health?token={}", ready.token),
        Duration::from_secs(15),
    );
    if !matches!(health, Ok((204, _))) {
        eprintln!("health check failed: {health:?}");
        shared.shutdown();
        return 5;
    }
    if !shared.shutdown() {
        eprintln!("the server did not exit cleanly when its stdin closed");
        return 6;
    }
    println!(
        "ok: Plexora {} started in {:.1}s on {} and stopped cleanly",
        ready.version,
        boot.as_secs_f64(),
        ready.origin
    );
    0
}

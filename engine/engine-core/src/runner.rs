//! `engine run`: the full assembly.
//!
//! Every part comes from `assembly.rs`. While a crate is still empty its
//! constructor returns "not wired yet", so this command starts, names the
//! missing part, and stops without writing to the log or touching a venue.
//!
//! Two claims are staked before any of it runs: the venue account, so this
//! engine is not sending orders beside the Python fleet, and the log file, so
//! it is not appending beside another engine. Both are kernel locks that die
//! with the process.

use std::error::Error;
use std::path::Path;

use engine_types::{AccountIdentity, Strategy, VenueGateway};
use engine_venue::lease::{self, AccountLease, LeaseError};
use engine_venue::Venue;

use crate::assembly;
use crate::config;
use crate::engine::Engine;

/// What the engine writes into the lease file, so an operator who finds the
/// account held knows which of the fleet's programs is holding it. The Python
/// side spells its own roles the same way — `ledger_reset`, and so on.
const LEASE_ROLE: &str = "engine";

pub async fn run(config_path: &Path) -> Result<(), Box<dyn Error>> {
    let loaded = config::load(config_path)?;
    let settings = loaded.config.engine.clone();
    tracing::info!(
        config = %config_path.display(),
        hash = %loaded.sha256,
        venue = %settings.venue,
        strategies = loaded.config.strategies.len(),
        "starting"
    );
    tracing::warn!("orders will be sent, and the risk kernel gates every one of them");

    // Compilation and request-shape tests are not production evidence. Keep
    // this before the log claim and before any credential or socket is opened;
    // testnet realms remain runnable specifically so they can earn canary
    // evidence without real capital.
    let chosen = assembly::venue_name(&settings.venue)?;
    chosen.require_engine_run_ready()?;

    // Building a strategy is reading its config block: no clock, no socket,
    // no decision. Nothing below has happened yet when the lease is taken.
    let strategies: Vec<Box<dyn Strategy>> = assembly::strategies(&loaded.config.strategies)?;
    // Each block's own name, so the log and the heartbeat can tell two sleeves
    // running the same plug apart. Both of this fleet's are `target_book`.
    let sleeves: Vec<String> = loaded
        .config
        .strategies
        .iter()
        .map(|s| s.sleeve_name().to_string())
        .collect();
    let wanted: Vec<_> = strategies
        .iter()
        .flat_map(|s| s.subscriptions())
        .collect::<Vec<_>>();
    let risk = assembly::risk(&loaded.config.risk)?;
    // Checked here, while the strategies are still in hand and before the
    // account lease is taken: a config that wires a book to the wrong plug is
    // a mistake to make at the door, not after claiming an account.
    let books = assembly::target_books(&settings, &loaded.config.strategies, &strategies)?;

    // The log is claimed and replayed before anything is given a symbol
    // table, because the table STARTS from the log: the previous run's own
    // id order, then this config's subscriptions on top. Claim before open,
    // because opening truncates a torn tail — doing that to a log another
    // engine is appending to is the damage the claim prevents. Shadow runs
    // take it too: they write the same log.
    let _log_claim = engine_wal::lock(&settings.wal_path)?;
    let (wal, replayed) = assembly::wal(&settings.wal_path)?;
    let symbols = assembly::symbol_order(&replayed, &wanted);

    // The switch, turned once. All three of the venue's parts are built from
    // this one value, so a config cannot half-switch — send orders to one
    // venue and price them off another's book.
    let mut venue = assembly::venue(chosen, symbols.clone())?;

    // Held for the whole run. Dropped at the end of this function, and by the
    // kernel if the process dies first.
    let claimed = single_writer(&mut venue).await?;

    let mut market_feed =
        assembly::market_feed(chosen, &assembly::boot_subscriptions(&symbols, &wanted));
    let mut order_feed = assembly::order_feed(chosen, symbols)?;

    // Subscribe before any account/history snapshot. Once this readiness
    // watermark is consumed, boot recovery covers everything through its
    // REST endpoint and the live feed buffers everything after it. No order
    // can be admitted while the private stream is still making its first
    // dial or repeatedly failing authentication.
    order_feed.await_ready().await?;

    let mut engine = Engine::boot_as(
        &settings,
        &loaded.sha256,
        wal,
        risk,
        venue,
        strategies,
        &sleeves,
        &replayed,
    )
    .await?;

    engine.watch_targets(books);
    if let Some(trades) = assembly::trades(&settings) {
        engine.write_trades(trades);
    }
    if let Some(heartbeat) = assembly::heartbeat(
        &settings,
        claimed.account.clone(),
        claimed
            .lease
            .as_ref()
            .map(|lease| lease.path().to_path_buf()),
    ) {
        engine.write_heartbeat(heartbeat);
    }

    let outcome = engine
        .run(&mut market_feed, &mut order_feed, stop_signal())
        .await?;
    tracing::info!(?outcome, "stopped");
    Ok(())
}

/// Wait for whichever stop this engine is given.
///
/// systemd stops a service with SIGTERM; a terminal sends SIGINT. Both mean
/// the same thing here — finish the current write, drop the account lease,
/// exit zero. Answering only SIGINT makes every deploy a kill: the log's
/// buffered tail never reaches the OS, and systemd records the clean stop as
/// a failure, which pages.
///
/// The handler is registered when this is called, not when it is first
/// polled, so it is in place before the engine begins running.
fn stop_signal() -> impl std::future::Future<Output = ()> {
    let terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate());
    async move {
        match terminate {
            Ok(mut terminate) => {
                tokio::select! {
                    _ = tokio::signal::ctrl_c() => {}
                    _ = terminate.recv() => {}
                }
            }
            Err(error) => {
                tracing::warn!(%error, "no SIGTERM handler: this run stops on SIGINT only");
                let _ = tokio::signal::ctrl_c().await;
            }
        }
    }
}

/// What the run claimed before the engine booted: the account lock, when this
/// is a live run, and whose account it is. The heartbeat file names both, so
/// an operator who finds a stuck engine knows which account it is on.
struct Claim {
    lease: Option<AccountLease>,
    account: Option<AccountIdentity>,
}

/// Make sure nothing else is sending orders to this venue account.
///
/// The lock is the Python fleet's own — same directory, same file name, same
/// protocol — so the engine and the owner service contend with each other
/// rather than each holding a lock the other has never heard of. See
/// `engine_venue::lease`.
///
/// Take it before the engine boots, and refuse to start if somebody already
/// has it.
async fn single_writer(venue: &mut Venue) -> Result<Claim, Box<dyn Error>> {
    // Not knowing whose account this is means not knowing
    // which lock to take, which means not knowing who would be stepped on.
    let who = venue.account_identity().await?;
    match lease::acquire(&who.venue, &who.realm, &who.user_id, LEASE_ROLE) {
        Ok(lease) => {
            tracing::info!(
                venue = %who.venue,
                account = %who.user_id,
                realm = %who.realm,
                lease = %lease.path().display(),
                "this engine is the one writer for this account"
            );
            Ok(Claim {
                lease: Some(lease),
                account: Some(who),
            })
        }
        Err(LeaseError::AlreadyHeld { path, holder }) => {
            tracing::error!(
                account = %who.user_id,
                realm = %who.realm,
                lease = %path.display(),
                holder = holder.as_deref().unwrap_or("no note"),
                "refusing to start: something else is already sending orders to this account"
            );
            Err(Box::new(LeaseError::AlreadyHeld { path, holder }))
        }
        Err(e) => Err(Box::new(e)),
    }
}

#[cfg(test)]
mod tests {
    use std::time::Duration;

    #[tokio::test]
    async fn a_systemd_stop_reaches_the_shutdown_path() {
        // The handler is registered by the call below, before the raise:
        // an unregistered SIGTERM would kill this test binary outright.
        let stop = super::stop_signal();
        unsafe { libc::raise(libc::SIGTERM) };
        tokio::time::timeout(Duration::from_secs(5), stop)
            .await
            .expect("systemd stops with SIGTERM, and it has to end the run");
    }
}

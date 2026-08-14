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
use engine_venue::lease::{self, AccountLease, LeaseError, LeaseHolder};
use engine_venue::Venue;

use crate::assembly;
use crate::config;
use crate::engine::Engine;

/// What the engine writes into the lease file, so an operator who finds the
/// account held knows which of the fleet's programs is holding it. The Python
/// side spells its own roles the same way — `ledger_reset`, and so on.
const LEASE_ROLE: &str = "engine";

pub async fn run(config_path: &Path, live: bool) -> Result<(), Box<dyn Error>> {
    let loaded = config::load(config_path)?;
    let mut settings = loaded.config.engine.clone();
    if live {
        settings.shadow = false;
    }
    tracing::info!(
        config = %config_path.display(),
        hash = %loaded.sha256,
        shadow = settings.shadow,
        venue = %settings.venue,
        strategies = loaded.config.strategies.len(),
        "starting"
    );
    if !settings.shadow {
        tracing::warn!("live: orders will be sent, and the risk kernel gates every one of them");
    }

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
    let symbols = assembly::symbol_order(&wanted);
    let risk = assembly::risk(&loaded.config.risk, &loaded.config.strategies)?;
    // Checked here, while the strategies are still in hand and before the
    // account lease is taken: a config that wires a book to the wrong plug is
    // a mistake to make at the door, not after claiming an account.
    let books = assembly::target_books(&settings, &loaded.config.strategies, &strategies)?;
    let mut venue = assembly::venue(&settings.venue, symbols.clone())?;

    // Held for the whole run. Dropped at the end of this function, and by the
    // kernel if the process dies first.
    let claimed = single_writer(&mut venue, settings.shadow).await?;
    // Before the log is opened, because opening it truncates a torn tail —
    // doing that to a log another engine is appending to is the damage this
    // is here to prevent. Shadow runs take it too: they write the same log.
    let _log_claim = engine_wal::lock(&settings.wal_path)?;

    let mut market_feed = assembly::market_feed(&wanted);
    let mut order_feed = assembly::order_feed(venue.realm(), symbols)?;

    let (wal, replayed) = assembly::wal(&settings.wal_path)?;

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
    if let Some(heartbeat) = assembly::heartbeat(
        &settings,
        claimed.account.clone(),
        claimed.lease.as_ref().map(|lease| lease.path().to_path_buf()),
    ) {
        engine.write_heartbeat(heartbeat);
    }

    let outcome = engine
        .run(&mut market_feed, &mut order_feed, async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    tracing::info!(?outcome, "stopped");
    Ok(())
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
/// Live: take it before the engine boots, and refuse to start if somebody
/// already has it. Shadow: never take it. A shadow run sends nothing, and a
/// shadow run that held the lease would be locking out the writer that does.
/// It reports what it found instead.
async fn single_writer(venue: &mut Venue, shadow: bool) -> Result<Claim, Box<dyn Error>> {
    if shadow {
        // Information only, so nothing here stops the run: a shadow engine
        // that cannot reach the venue still has a log to write and a loop to
        // exercise.
        match venue.account_identity().await {
            Ok(who) => {
                match lease::probe(&who.realm, &who.user_id) {
                    Ok(LeaseHolder::Free) => tracing::info!(
                        account = %who.user_id,
                        realm = %who.realm,
                        "shadow: nothing holds this account; a live engine could start"
                    ),
                    Ok(LeaseHolder::Held { note }) => tracing::info!(
                        account = %who.user_id,
                        realm = %who.realm,
                        holder = note.as_deref().unwrap_or("no note"),
                        "shadow: something already holds this account"
                    ),
                    Err(e) => {
                        tracing::warn!(error = %e, "shadow: cannot tell who holds this account")
                    }
                }
                return Ok(Claim { lease: None, account: Some(who) });
            }
            Err(e) => {
                tracing::warn!(error = %e, "shadow: cannot ask the venue whose account this is");
                return Ok(Claim { lease: None, account: None });
            }
        }
    }

    // Live from here. Not knowing whose account this is means not knowing
    // which lock to take, which means not knowing who would be stepped on.
    let who = venue.account_identity().await?;
    match lease::acquire(&who.realm, &who.user_id, LEASE_ROLE) {
        Ok(lease) => {
            tracing::info!(
                account = %who.user_id,
                realm = %who.realm,
                lease = %lease.path().display(),
                "this engine is the one writer for this account"
            );
            Ok(Claim { lease: Some(lease), account: Some(who) })
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

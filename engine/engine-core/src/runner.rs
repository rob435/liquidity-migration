//! `engine run`: the full assembly.
//!
//! Every concrete part comes from `assembly.rs`. What `run` builds, in order:
//!
//! | # | Step | Built by |
//! | --- | --- | --- |
//! | 1 | config, hashed; venue name parsed once | `config::load`, `assembly::venue_name` |
//! | 2 | WAL claim, then the replay every later step reads | `engine_wal::lock`, `assembly::boot_wal` |
//! | 3 | identity plan: sleeve slots and the symbol table | `identities::plan_identities`, `assembly::symbol_order` |
//! | 4 | strategies, and the risk kernel they are gated by | `assembly::strategies_for_registry`, `assembly::risk` |
//! | 5 | venue gateway, on the name from 1 and the symbols from 3 | `assembly::venue` |
//! | 6 | account lease, held for the whole run | `single_writer` |
//! | 7 | public market feed | `assembly::market_feed_for_registry` |
//! | 8 | private order feed, ready before any order | `assembly::order_feed` |
//! | 9 | engine boot over the same replay | `Engine::boot_as_exact` |
//! | 10 | run loop, chosen by the spool paths the config names | `Engine::run*` |
//!
//! Two kernel locks are staked before the engine boots: the log file, so no
//! second engine appends to the same WAL, and the venue account, so this is
//! its only order writer. Both die with the process.

use std::error::Error;
use std::path::Path;

use engine_types::{AccountIdentity, MarketFeed, VenueGateway};
use engine_venue::lease::{self, AccountLease, LeaseError};
use engine_venue::{Venue, VenueReadiness};

use crate::assembly;
use crate::config;
use crate::engine::Engine;

/// What the engine writes into the lease file, so an operator who finds the
/// account held knows which program is holding it.
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
    // this before the log claim and before any credential or socket is opened.
    // A live-canary realm runs as the owner's forward test, on the realm
    // table's posture and the credential file's REAL_MONEY, and this line
    // names what the run has not yet been seen doing.
    let chosen = assembly::venue_name(&settings.venue)?;
    chosen.require_engine_run_ready()?;
    if chosen.readiness() == VenueReadiness::LiveCanary {
        let unproven: Vec<_> = chosen
            .unproven_capabilities()
            .iter()
            .map(|capability| capability.as_str())
            .collect();
        tracing::warn!(
            venue = %chosen,
            readiness = chosen.readiness().as_str(),
            unproven = unproven.join(", "),
            "forward test: this realm holds no current live receipt for these capabilities"
        );
    }

    let _log_claim = engine_wal::lock(&settings.wal_path)?;
    let (wal, replayed) = assembly::boot_wal(&settings.wal_path)?;
    let configured_keys: Vec<_> = loaded
        .config
        .strategies
        .iter()
        .map(|strategy| strategy.sleeve_name().to_string())
        .collect();
    let plan = crate::identities::plan_identities(
        &replayed,
        &configured_keys,
        None,
        &Default::default(),
        &[],
    )?;
    let strategies =
        assembly::strategies_for_registry(&loaded.config.strategies, &plan, &replayed)?;
    if strategies
        .iter()
        .any(|strategy| strategy.requires_signal_feed())
        && settings.signal_spool_path.is_none()
    {
        return Err("a configured strategy requires engine.signal_spool_path".into());
    }
    let sleeves: Vec<_> = plan
        .state
        .sleeves
        .iter()
        .map(|key| key.as_str().to_string())
        .collect();

    // Before the venue is built, so a sleeve that needs something this
    // adapter does not do ends the run rather than trading with one of its
    // verbs permanently refused. Still after the log claim: the identity plan
    // is what names the sleeves.
    for sleeve in assembly::compatibility(chosen, &strategies, &sleeves)?.sleeves {
        if sleeve.unproven.is_empty() {
            continue;
        }
        let unproven: Vec<_> = sleeve
            .unproven
            .iter()
            .map(|capability| capability.as_str())
            .collect();
        tracing::warn!(
            sleeve = %sleeve.sleeve,
            plug = %sleeve.plug,
            unproven = unproven.join(", "),
            "forward test: this sleeve's execution requirements hold no current live receipt on this realm"
        );
    }

    let mut wanted: Vec<_> = strategies
        .iter()
        .flat_map(|strategy| strategy.subscriptions())
        .collect();
    let risk = assembly::risk(&loaded.config.risk)?;
    let catalog = crate::engine::symbol_admission::replay_catalog(&replayed)?;
    for subscription in crate::signals::active_subscriptions_listed(&replayed, catalog.as_deref()) {
        if !wanted.contains(&subscription) {
            wanted.push(subscription);
        }
    }
    for subscription in crate::portfolio_routes::replayed(&replayed)? {
        if !wanted.contains(&subscription) {
            wanted.push(subscription);
        }
    }
    let symbols = assembly::symbol_order(&replayed, &wanted)?;
    if symbols.len() > engine_types::identity::DENSE_ID_CAPACITY {
        return Err(engine_types::identity::IdentityError::SymbolIdsExhausted.into());
    }

    // The switch, turned once. All three of the venue's parts are built from
    // this one value, so a config cannot half-switch — send orders to one
    // venue and price them off another's book.
    let mut venue = assembly::venue(chosen, symbols.clone())?;

    // Held for the whole run. Dropped at the end of this function, and by the
    // kernel if the process dies first.
    let claimed = single_writer(&mut venue).await?;

    let mut market_feed = assembly::market_feed_for_registry(chosen, &symbols, &wanted)?;
    let mut order_feed = assembly::order_feed(chosen, symbols)?;

    // Subscribe before any account/history snapshot. Once this readiness
    // watermark is consumed, boot recovery covers everything through its
    // REST endpoint and the live feed buffers everything after it. No order
    // can be admitted while the private stream is still making its first
    // dial or repeatedly failing authentication.
    order_feed.await_ready().await?;

    let mut engine = Engine::boot_replay_exact(
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
    // The decoded log is boot input; the live loop must not hold it.
    drop(replayed);

    for subscription in engine.subscriptions() {
        let expected = engine
            .market()
            .table
            .get(&subscription.symbol)
            .ok_or("boot route has no durable symbol")?;
        if market_feed.admit(&subscription.symbol, subscription.feed) != Some(expected) {
            return Err(format!(
                "public feed refused retained portfolio route for {}",
                subscription.symbol
            )
            .into());
        }
    }

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

    let outcome = match (
        settings.signal_spool_path.as_deref(),
        settings.control_spool_path.as_deref(),
    ) {
        (Some(signal_path), Some(control_path)) => {
            let mut signals = crate::signals::HybridSignalFeed::for_directory(signal_path);
            let mut controls = crate::controls::SpoolRuntimeControlFeed::new(control_path);
            engine
                .run_with_inputs(
                    &mut market_feed,
                    &mut order_feed,
                    &mut signals,
                    &mut controls,
                    stop_signal(),
                )
                .await?
        }
        (Some(signal_path), None) => {
            let mut signals = crate::signals::HybridSignalFeed::for_directory(signal_path);
            engine
                .run_with_signals(
                    &mut market_feed,
                    &mut order_feed,
                    &mut signals,
                    stop_signal(),
                )
                .await?
        }
        (None, Some(control_path)) => {
            let mut signals = crate::signals::NoSignals;
            let mut controls = crate::controls::SpoolRuntimeControlFeed::new(control_path);
            engine
                .run_with_inputs(
                    &mut market_feed,
                    &mut order_feed,
                    &mut signals,
                    &mut controls,
                    stop_signal(),
                )
                .await?
        }
        (None, None) => {
            engine
                .run(&mut market_feed, &mut order_feed, stop_signal())
                .await?
        }
    };
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
/// The lock uses the account-owner lease protocol in `engine_venue::lease`, so
/// every engine process for the same venue account contends on one identity.
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

    // Wall clock: the signal reaches the runtime through the I/O driver, and a
    // paused clock jumps to the timeout the moment the runtime idles.
    #[tokio::test(start_paused = true)]
    async fn a_systemd_stop_reaches_the_shutdown_path() {
        let _io = crate::test_io::IoProgress::new();
        // The handler is registered by the call below, before the raise:
        // an unregistered SIGTERM would kill this test binary outright.
        let stop = super::stop_signal();
        unsafe { libc::raise(libc::SIGTERM) };
        tokio::time::timeout(Duration::from_secs(5), stop)
            .await
            .expect("systemd stops with SIGTERM, and it has to end the run");
    }
}

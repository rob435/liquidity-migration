//! Every number the kernel judges against. Nothing is a constant in code: the
//! Python defaults these were ported from are recorded in PORT_NOTES.md and
//! must be supplied by the caller.

use engine_types::ids::StrategyId;

/// A config the kernel refuses to run on. Mirrors the `ValueError`s the Python
/// profile loader and the loss guard raise at construction.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ConfigError {
    pub detail: String,
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.detail)
    }
}

impl std::error::Error for ConfigError {}

fn bad(detail: impl Into<String>) -> ConfigError {
    ConfigError {
        detail: detail.into(),
    }
}

fn positive(value: f64, name: &str) -> Result<(), ConfigError> {
    if !value.is_finite() || value <= 0.0 {
        return Err(bad(format!("{name} must be positive")));
    }
    Ok(())
}

/// Daily loss ceiling against the UTC day's opening equity.
#[derive(Clone, Debug, PartialEq)]
pub struct LossGuardConfig {
    /// `None` disables the ceiling; the guard still runs and still refuses on
    /// blindness. Must be positive when set.
    pub max_daily_loss_usdt: Option<f64>,
}

impl LossGuardConfig {
    fn validate(&self) -> Result<(), ConfigError> {
        match self.max_daily_loss_usdt {
            None => Ok(()),
            Some(ceiling) => positive(ceiling, "max_daily_loss_usdt"),
        }
    }
}

/// The equity-anchored envelope: a capital reference that follows the wallet,
/// and the worst-case-loss allowance derived from it.
#[derive(Clone, Debug, PartialEq)]
pub struct EnvelopeConfig {
    /// False pins the reference at `reference_usdt` forever (the demo profile).
    pub tracks_equity: bool,
    /// The reference the allocations below were sized against.
    pub reference_usdt: f64,
    /// Share of equity the reference tracks. In (0, 1].
    pub equity_fraction: f64,
    /// The reference never falls below this.
    pub floor_usdt: f64,
    /// Expansion needs a move larger than this; contraction is immediate.
    pub expand_dead_band_fraction: f64,
    /// Account gross notional cap, as a multiple of the reference.
    pub gross_notional_multiple: f64,
    /// How far a position can move against us before its stop ends it. Turns a
    /// notional cap into a worst-case-loss allowance.
    pub disaster_stop_fraction: f64,
    /// Most gross notional any one symbol may carry, across the whole account.
    /// One strategy owns a symbol, so nothing else stops it concentrating
    /// every share it has into a single name.
    pub max_symbol_notional_usdt: f64,
    /// A second account-wide gross ceiling, below the gross cap the allowance
    /// above is derived from. Set the two equal and this one never binds.
    pub max_component_gross_notional_usdt: f64,
    /// Most margin the whole book may commit. The partition's per-strategy
    /// margin shares are carved out of this.
    pub max_initial_margin_usdt: f64,
}

impl EnvelopeConfig {
    fn validate(&self) -> Result<(), ConfigError> {
        positive(self.reference_usdt, "reference_usdt")?;
        positive(self.floor_usdt, "floor_usdt")?;
        positive(self.gross_notional_multiple, "gross_notional_multiple")?;
        if !self.equity_fraction.is_finite() || self.equity_fraction <= 0.0 {
            return Err(bad("equity_fraction must be positive"));
        }
        if self.equity_fraction > 1.0 {
            return Err(bad("equity_fraction cannot exceed 1"));
        }
        if !self.expand_dead_band_fraction.is_finite() || self.expand_dead_band_fraction < 0.0 {
            return Err(bad("expand_dead_band_fraction must not be negative"));
        }
        if !self.disaster_stop_fraction.is_finite()
            || !(self.disaster_stop_fraction > 0.0 && self.disaster_stop_fraction < 1.0)
        {
            return Err(bad("disaster_stop_fraction must be a fraction in (0, 1)"));
        }
        positive(self.max_symbol_notional_usdt, "max_symbol_notional_usdt")?;
        positive(
            self.max_component_gross_notional_usdt,
            "max_component_gross_notional_usdt",
        )?;
        positive(self.max_initial_margin_usdt, "max_initial_margin_usdt")?;
        // operational_profile.py proves the same nesting at load. Caps that do
        // not nest describe a book nobody can reach: the outer one would never
        // bind, and an operator tightening it would see nothing change.
        if self.max_symbol_notional_usdt > self.max_component_gross_notional_usdt {
            return Err(bad(
                "max_symbol_notional_usdt cannot exceed max_component_gross_notional_usdt",
            ));
        }
        if self.max_component_gross_notional_usdt > self.account_gross_cap_usdt() {
            return Err(bad(
                "max_component_gross_notional_usdt cannot exceed reference_usdt * \
                 gross_notional_multiple",
            ));
        }
        if self.max_initial_margin_usdt > self.reference_usdt {
            return Err(bad(
                "max_initial_margin_usdt cannot exceed reference_usdt",
            ));
        }
        Ok(())
    }

    /// The account-wide gross notional cap at the configured reference.
    pub fn account_gross_cap_usdt(&self) -> f64 {
        self.reference_usdt * self.gross_notional_multiple
    }
}

/// One strategy's private share of the envelope, in USDT at the envelope's
/// configured reference. Both bounds scale with the reference.
#[derive(Clone, Debug, PartialEq)]
pub struct StrategyAllocation {
    pub strategy: StrategyId,
    pub max_gross_notional_usdt: f64,
    pub max_initial_margin_usdt: f64,
}

/// The per-strategy capital partition. Empty means unpartitioned: the account
/// caps alone bind, exactly as an unpartitioned Python profile behaves.
#[derive(Clone, Debug, PartialEq)]
pub struct PartitionConfig {
    pub allocations: Vec<StrategyAllocation>,
    /// Account leverage, used to turn gross notional into initial margin.
    pub leverage: f64,
    /// An order smaller than this after clamping is refused, not sent.
    pub min_order_notional_usdt: f64,
}

impl PartitionConfig {
    fn validate(&self, envelope: &EnvelopeConfig) -> Result<(), ConfigError> {
        positive(self.leverage, "partition leverage")?;
        if !self.min_order_notional_usdt.is_finite() || self.min_order_notional_usdt < 0.0 {
            return Err(bad("min_order_notional_usdt must not be negative"));
        }
        let mut seen: Vec<u16> = Vec::new();
        for share in &self.allocations {
            positive(
                share.max_gross_notional_usdt,
                "allocation max_gross_notional_usdt",
            )?;
            positive(
                share.max_initial_margin_usdt,
                "allocation max_initial_margin_usdt",
            )?;
            if seen.contains(&share.strategy.0) {
                return Err(bad("a strategy is named twice in the partition"));
            }
            seen.push(share.strategy.0);
        }
        if self.allocations.is_empty() {
            return Ok(());
        }
        let gross: f64 = self
            .allocations
            .iter()
            .map(|share| share.max_gross_notional_usdt)
            .sum();
        if gross > envelope.account_gross_cap_usdt() * (1.0 + 1e-12) {
            return Err(bad(
                "partition shares sum above the account gross notional cap",
            ));
        }
        let margin: f64 = self
            .allocations
            .iter()
            .map(|share| share.max_initial_margin_usdt)
            .sum();
        // Against the declared account margin cap, as _parse_sleeve_limits does.
        // Before that cap was a config key this compared against the gross cap
        // divided by leverage, which is a different number: the mainnet shares
        // sum to exactly the declared cap and would have been refused by it.
        if margin > envelope.max_initial_margin_usdt * (1.0 + 1e-12) {
            return Err(bad(
                "partition margin shares sum above max_initial_margin_usdt",
            ));
        }
        Ok(())
    }

    pub fn share(&self, strategy: StrategyId) -> Option<&StrategyAllocation> {
        self.allocations
            .iter()
            .find(|share| share.strategy == strategy)
    }
}

/// Everything the kernel needs, supplied once at boot.
#[derive(Clone, Debug, PartialEq)]
pub struct KernelConfig {
    /// An account view older than this is not evidence about the account now.
    pub max_account_view_age_ns: u64,
    pub loss_guard: LossGuardConfig,
    pub envelope: EnvelopeConfig,
    pub partition: PartitionConfig,
    /// Quantities at or below this are treated as zero.
    pub qty_tolerance: f64,
}

impl KernelConfig {
    pub fn validate(&self) -> Result<(), ConfigError> {
        self.loss_guard.validate()?;
        self.envelope.validate()?;
        self.partition.validate(&self.envelope)?;
        if !self.qty_tolerance.is_finite() || self.qty_tolerance < 0.0 {
            return Err(bad("qty_tolerance must not be negative"));
        }
        Ok(())
    }
}

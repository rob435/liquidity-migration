//! Every number the kernel judges against. Nothing is a constant in code: the
//! Python defaults these were ported from are recorded in PORT_NOTES.md and
//! must be supplied by the caller.

/// A config the kernel refuses to run on. Raised at construction, so a bad
/// number never reaches a decision.
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

/// The equity-anchored envelope: a capital reference that follows the wallet,
/// and the worst-case-loss allowance derived from it.
#[derive(Clone, Debug, PartialEq)]
pub struct EnvelopeConfig {
    /// False pins the reference at `reference_usdt` forever (the demo profile).
    pub tracks_equity: bool,
    /// The reference every cap below was sized against.
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
    /// A second account-wide gross ceiling, below the gross cap the allowance
    /// above is derived from. Set the two equal and this one never binds.
    pub max_component_gross_notional_usdt: f64,
    /// Most margin the whole book may commit.
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
        positive(
            self.max_component_gross_notional_usdt,
            "max_component_gross_notional_usdt",
        )?;
        positive(self.max_initial_margin_usdt, "max_initial_margin_usdt")?;
        // The tolerance is not slack, it is arithmetic. The account cap is
        // held as a multiple and rebuilt as `reference * multiple`, while this
        // number was read straight from the profile — and both shipped
        // profiles set the two equal. Rebuilding 201 as 100 * 2.01 gives
        // 200.99999999999997, so an exact comparison would refuse a profile
        // for saying the same thing twice. The two shipped files happen to use
        // ratios that survive a round trip (1.75, 2.0); most numbers do not.
        if self.max_component_gross_notional_usdt > self.account_gross_cap_usdt() * (1.0 + 1e-12) {
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

/// Everything the kernel needs, supplied once at boot.
#[derive(Clone, Debug, PartialEq)]
pub struct KernelConfig {
    /// An account view older than this is not evidence about the account now.
    pub max_account_view_age_ns: u64,
    pub envelope: EnvelopeConfig,
    /// Account leverage, used to turn gross notional into initial margin.
    pub leverage: f64,
    /// Quantities at or below this are treated as zero.
    pub qty_tolerance: f64,
}

impl KernelConfig {
    pub fn validate(&self) -> Result<(), ConfigError> {
        self.envelope.validate()?;
        positive(self.leverage, "leverage")?;
        if !self.qty_tolerance.is_finite() || self.qty_tolerance < 0.0 {
            return Err(bad("qty_tolerance must not be negative"));
        }
        // operational_profile.py:409, the one load-time proof PORT_NOTES had
        // recorded as not ported. It needs both blocks, which is why it lives
        // here rather than in either one.
        //
        // Gross above the whole capital reference times leverage is gross
        // nobody can reach — that is the most book the account could carry if
        // every last unit of capital were posted as margin. A cap set above it
        // looks like a limit and is scenery: an operator tightening it would
        // watch nothing change.
        //
        // It says nothing about `max_initial_margin_usdt`, which may sit well
        // below the reference on purpose. A profile that wants margin to be
        // the binding cap is a profile, not a mistake.
        let reachable = self.envelope.reference_usdt * self.leverage;
        if self.envelope.account_gross_cap_usdt() > reachable * (1.0 + 1e-12) {
            return Err(bad(format!(
                "the account gross cap ({:.6}) is above what the capital reference could \
                 fund at leverage {:.6} ({reachable:.6}); that much book cannot be reached",
                self.envelope.account_gross_cap_usdt(),
                self.leverage,
            )));
        }
        Ok(())
    }
}

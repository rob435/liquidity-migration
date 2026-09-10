//! The one list of execution behaviours a venue either does or does not do.
//!
//! Here rather than in `engine-public` beside the evidence rows because both
//! sides of the question need it: a realm's row says what the venue has been
//! seen doing, and [`crate::Strategy::execution_requirements`] says what a
//! sleeve's own actions need. `engine-public` re-exports this type, so the
//! rows and the matrix keep their import path.

/// One execution behaviour a realm is qualified for on its own evidence.
///
/// Evidence is per behaviour because the venue's answers are: a realm that has
/// been seen accepting and cancelling an order has been seen doing exactly
/// that, and nothing about fills, protection or recovery follows from it. The
/// readiness label is a summary of these and never a label of its own.
#[derive(Copy, Clone, PartialEq, Eq, Debug, Hash)]
pub enum Capability {
    Submit,
    Cancel,
    PostOnly,
    FillAttribution,
    PartialFill,
    Amend,
    ExactQuantity,
    ReduceBelowMinimum,
    ProtectionPlace,
    ProtectionChange,
    ProtectionTrigger,
    ReconnectHistoryRecovery,
    FundingFeeCash,
}

impl Capability {
    /// The one list, walked by the completeness checks and by the doc table.
    pub const ALL: [Capability; 13] = [
        Capability::Submit,
        Capability::Cancel,
        Capability::PostOnly,
        Capability::FillAttribution,
        Capability::PartialFill,
        Capability::Amend,
        Capability::ExactQuantity,
        Capability::ReduceBelowMinimum,
        Capability::ProtectionPlace,
        Capability::ProtectionChange,
        Capability::ProtectionTrigger,
        Capability::ReconnectHistoryRecovery,
        Capability::FundingFeeCash,
    ];

    /// Stable spelling: it is a column key in `docs/engine.md` §2 and appears
    /// in the boot refusal an operator reads.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Submit => "submit",
            Self::Cancel => "cancel",
            Self::PostOnly => "post-only",
            Self::FillAttribution => "fill-attribution",
            Self::PartialFill => "partial-fill",
            Self::Amend => "amend",
            Self::ExactQuantity => "exact-quantity",
            Self::ReduceBelowMinimum => "reduce-below-minimum",
            Self::ProtectionPlace => "protection-place",
            Self::ProtectionChange => "protection-change",
            Self::ProtectionTrigger => "protection-trigger",
            Self::ReconnectHistoryRecovery => "reconnect-history-recovery",
            Self::FundingFeeCash => "funding-fee-cash",
        }
    }
}

impl std::fmt::Display for Capability {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

use super::common::*;
use engine_risk::Kernel;
use engine_types::numeric::Exact;
use engine_types::portfolio::PortfolioState;
use engine_types::risk::PortfolioRiskVerdict;
use engine_types::{AccountView, DenyReason, Intent, RiskKernel, RiskVerdict, Side};

#[derive(Clone, Copy, Debug)]
enum Admission {
    New,
    Portfolio,
    Queued,
    Amend,
}
const ADMISSIONS: [Admission; 4] = [
    Admission::New,
    Admission::Portfolio,
    Admission::Queued,
    Admission::Amend,
];

fn assess(
    mode: Admission,
    intent: &Intent,
    account: &AccountView,
    now_ns: u64,
) -> (Result<Exact, DenyReason>, f64) {
    let mut kernel = Kernel::new(equity_tracking_config()).unwrap();
    if matches!(mode, Admission::Queued | Admission::Amend) {
        kernel.register_order_with_account("queued", intent, intent.qty, account);
    }
    let result = match mode {
        Admission::New | Admission::Amend => match if matches!(mode, Admission::Amend) {
            kernel.assess_price_amend("queued", intent, account, now_ns)
        } else {
            kernel.assess(intent, account, now_ns)
        } {
            RiskVerdict::Allow { qty } => Ok(Exact::from_legacy_f64(qty).unwrap()),
            RiskVerdict::Deny { reason } => Err(reason),
        },
        Admission::Portfolio | Admission::Queued => match if matches!(mode, Admission::Queued) {
            kernel.reassess_portfolio_order(
                "queued",
                intent,
                account,
                &PortfolioState::default(),
                now_ns,
            )
        } else {
            kernel.assess_portfolio(intent, account, &PortfolioState::default(), now_ns)
        } {
            PortfolioRiskVerdict::Allow { qty, .. } => Ok(qty),
            PortfolioRiskVerdict::Deny { reason } => Err(reason),
        },
    };
    (result, kernel.capital_reference_usdt())
}

#[test]
fn admission_clock_accepts_a_refreshed_account_and_uses_its_current_equity() {
    let intent = entry(CARRY, BUSDT, Side::Buy, 10_000.0, 10.0, 9.0, 100 * SEC);
    let original = serde_json::to_vec(&intent).unwrap();
    for mode in ADMISSIONS {
        let (result, capital) = assess(mode, &intent, &flat(100_000.0, 105 * SEC), 110 * SEC);
        assert_eq!(result, Ok(Exact::from_i64(10_000)), "{mode:?}");
        assert_eq!(capital, 100_000.0, "{mode:?}");
        let (poor, _) = assess(mode, &intent, &flat(10_000.0, 105 * SEC), 110 * SEC);
        assert!(
            matches!(poor, Err(DenyReason::EnvelopeBreached { .. })),
            "current equity must bind {mode:?}: {poor:?}"
        );
    }
    assert_eq!(serde_json::to_vec(&intent).unwrap(), original);
}

#[test]
fn admission_clock_refuses_an_account_that_aged_while_the_decision_waited() {
    let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, 100 * SEC);
    for mode in ADMISSIONS {
        let (result, _) = assess(mode, &intent, &flat(250_000.0, 100 * SEC), 221 * SEC);
        assert_eq!(
            result,
            Err(DenyReason::StaleAccountView {
                age_ns: 121 * SEC,
                max_age_ns: MAX_VIEW_AGE_NS
            }),
            "{mode:?}"
        );
    }
}

#[test]
fn admission_clock_refuses_a_truly_future_account_even_with_a_later_decision_stamp() {
    for decided_ns in [100 * SEC, 999 * SEC] {
        let intent = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, decided_ns);
        for mode in ADMISSIONS {
            let (result, _) = assess(mode, &intent, &flat(250_000.0, 110 * SEC + 1), 110 * SEC);
            assert!(
                matches!(result, Err(DenyReason::UnknownState { .. })),
                "{mode:?}: {result:?}"
            );
        }
    }
}

#[test]
fn admission_clock_accepts_a_replayed_prior_process_decision_without_rewriting_it() {
    let original = entry(CARRY, BUSDT, Side::Buy, 1.0, 10.0, 9.0, 999 * SEC);
    let bytes = serde_json::to_vec(&original).unwrap();
    let replayed: Intent = serde_json::from_slice(&bytes).unwrap();
    for mode in ADMISSIONS {
        let (result, _) = assess(mode, &replayed, &flat(250_000.0, 105 * SEC), 110 * SEC);
        assert_eq!(result, Ok(Exact::one()), "{mode:?}");
    }
    assert_eq!(serde_json::to_vec(&replayed).unwrap(), bytes);
}

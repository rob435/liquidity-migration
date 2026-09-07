use std::collections::BTreeSet;

use engine_types::{Action, Feed, Subscription, SymbolId, WalRecord};

pub(crate) fn action_symbol(action: &Action) -> Option<SymbolId> {
    match action {
        Action::Place(intent) => Some(intent.symbol),
        Action::Cancel { symbol, .. }
        | Action::Amend { symbol, .. }
        | Action::SetStop { symbol, .. } => Some(*symbol),
        _ => None,
    }
}

pub(crate) fn routes(
    symbols: impl IntoIterator<Item = SymbolId>,
    name: impl Fn(SymbolId) -> Option<String>,
) -> Result<Vec<Subscription>, String> {
    let mut unique = BTreeSet::new();
    for symbol in symbols {
        unique.insert(symbol);
    }
    let mut out = Vec::new();
    for symbol in unique {
        let symbol = name(symbol).ok_or("portfolio route names an unknown durable symbol")?;
        out.extend(
            [Feed::Quote, Feed::Depth]
                .into_iter()
                .map(|feed| Subscription {
                    symbol: symbol.clone(),
                    feed,
                }),
        );
    }
    Ok(out)
}

pub(crate) fn replayed(records: &[WalRecord]) -> Result<Vec<Subscription>, String> {
    let identities = crate::identities::replay_identities(records)
        .map_err(|error| error.to_string())?
        .unwrap_or_default();
    let attribution = crate::attribution::Attribution::try_from_records(records)?;
    let orders = crate::inflight::LedgerOfOrders::try_from_records(records)?;
    let controls = crate::portfolio_control::PortfolioControls::replay(records)?;
    let effects = crate::effects::Effects::replay(records, identities.sleeves.len())?;
    let stops = crate::reconcile::intended_stops(records)?;
    let physical = crate::reconcile::physical_exposure(records)?;
    routes(
        attribution
            .all_symbols()
            .chain(orders.iter_in_flight().map(|order| order.request.symbol))
            .chain(controls.exits.values().map(|exit| exit.symbol))
            .chain(controls.emergencies.keys().copied())
            .chain(controls.native_pending.keys().copied())
            .chain(stops.keys().copied())
            .chain(
                physical
                    .iter()
                    .filter(|(_, qty)| !qty.is_zero())
                    .map(|(symbol, _)| *symbol),
            )
            .chain(effects.transitions.values().flat_map(|transition| {
                transition
                    .effects
                    .iter()
                    .enumerate()
                    .filter(|(index, _)| !transition.completed.contains(index))
                    .filter_map(|(_, action)| action_symbol(action))
            })),
        |id| {
            identities
                .instruments
                .get(id.idx())
                .map(|row| row.symbol.clone())
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;

    fn prior_routes(
        symbols: impl IntoIterator<Item = SymbolId>,
        name: impl Fn(SymbolId) -> Option<String>,
    ) -> Result<Vec<Subscription>, String> {
        let mut out = Vec::new();
        for symbol in symbols.into_iter().collect::<BTreeSet<_>>() {
            let symbol = name(symbol).ok_or("portfolio route names an unknown durable symbol")?;
            out.extend(
                [Feed::Quote, Feed::Depth]
                    .into_iter()
                    .map(|feed| Subscription {
                        symbol: symbol.clone(),
                        feed,
                    }),
            );
        }
        Ok(out)
    }

    #[test]
    fn route_inputs_match_prior_order_duplicates_and_first_unknown() {
        let cases = [
            vec![],
            vec![5, 2, 0, 2, 5],
            (0..270).rev().chain(0..270).collect(),
        ];
        for symbols in cases {
            for unknown in [vec![], vec![0], vec![2, 5], vec![268, 269]] {
                let actual_calls = RefCell::new(Vec::new());
                let prior_calls = RefCell::new(Vec::new());
                let actual = routes(
                    symbols.iter().copied().map(SymbolId).inspect(|id| {
                        actual_calls.borrow_mut().push(("input", *id));
                    }),
                    |id| {
                        actual_calls.borrow_mut().push(("name", id));
                        (!unknown.contains(&id.0)).then(|| format!("S{:03}", 270 - id.0))
                    },
                );
                let prior = prior_routes(
                    symbols.iter().copied().map(SymbolId).inspect(|id| {
                        prior_calls.borrow_mut().push(("input", *id));
                    }),
                    |id| {
                        prior_calls.borrow_mut().push(("name", id));
                        (!unknown.contains(&id.0)).then(|| format!("S{:03}", 270 - id.0))
                    },
                );
                assert_eq!(actual, prior, "symbols={symbols:?}, unknown={unknown:?}");
                assert_eq!(actual_calls, prior_calls);
                if let Some(first_unknown) = symbols.iter().filter(|id| unknown.contains(id)).min()
                {
                    assert!(actual.is_err());
                    assert_eq!(
                        actual_calls.borrow().last(),
                        Some(&("name", SymbolId(*first_unknown)))
                    );
                }
            }
        }
    }
}

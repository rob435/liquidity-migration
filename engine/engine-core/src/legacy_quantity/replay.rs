use std::collections::{BTreeSet, HashMap, VecDeque};

use engine_types::{
    LegacyPhysicalQuantityCorrection, LegacySleeveQuantityCorrection, OrderRequest, OrderUpdate,
    StrategyId, SymbolId, WalRecord,
};

use crate::attribution::Attribution;

type Owner = (StrategyId, SymbolId);

pub(crate) enum Event<'a> {
    Cut {
        sleeves: Vec<LegacySleeveQuantityCorrection>,
        physical: &'a [LegacyPhysicalQuantityCorrection],
        symbol: Option<SymbolId>,
        terminal: bool,
        validate_terminal: bool,
    },
    Record(Record<'a>),
}

pub(crate) enum Record<'a> {
    Borrowed(&'a WalRecord),
    Owned(&'a WalRecord, Box<WalRecord>),
}

impl AsRef<WalRecord> for Record<'_> {
    fn as_ref(&self) -> &WalRecord {
        match self {
            Self::Borrowed(record) => record,
            Self::Owned(_, record) => record,
        }
    }
}

impl Record<'_> {
    pub(crate) fn original(&self) -> Option<&WalRecord> {
        match self {
            Self::Borrowed(_) => None,
            Self::Owned(record, _) => Some(record),
        }
    }
}

pub(crate) fn canonical_symbol(record: &WalRecord) -> Option<SymbolId> {
    match record {
        WalRecord::OrderUpdate {
            update:
                OrderUpdate::Fill {
                    symbol,
                    amounts,
                    allocation,
                    ..
                },
            ..
        } if amounts.is_some() || allocation.is_some() => Some(*symbol),
        WalRecord::RecoveredFill {
            symbol,
            amounts,
            allocation,
            ..
        } if amounts.is_some() || allocation.is_some() => Some(*symbol),
        _ => None,
    }
}

pub(crate) struct Replay<'a> {
    records: &'a [WalRecord],
    pending: Option<&'a WalRecord>,
    index: usize,
    end: usize,
    anchor: usize,
    context: Option<&'a WalRecord>,
    raw: Attribution,
    normalized: Attribution,
    sender: HashMap<&'a str, &'a OrderRequest>,
    names: Vec<String>,
    used: BTreeSet<Owner>,
    pub(crate) discovered: BTreeSet<Owner>,
    queued: VecDeque<Event<'a>>,
    done: bool,
    planning: bool,
}

impl<'a> Replay<'a> {
    pub(crate) fn new(
        records: &'a [WalRecord],
        pending: Option<&'a WalRecord>,
    ) -> Result<Self, String> {
        Self::with_planning(records, pending, false)
    }

    pub(crate) fn with_planning(
        records: &'a [WalRecord],
        pending: Option<&'a WalRecord>,
        planning: bool,
    ) -> Result<Self, String> {
        let mut replay = Self {
            records,
            pending,
            index: 0,
            end: 0,
            anchor: 0,
            context: None,
            raw: Attribution::default(),
            normalized: Attribution::default(),
            sender: HashMap::new(),
            names: Vec::new(),
            used: BTreeSet::new(),
            discovered: BTreeSet::new(),
            queued: VecDeque::new(),
            done: false,
            planning,
        };
        replay.span()?;
        Ok(replay)
    }

    fn span(&mut self) -> Result<(), String> {
        self.end = self.records[self.index..]
            .iter()
            .position(|r| matches!(r, WalRecord::LegacyQuantityGridAdopted { .. }))
            .map_or(self.records.len(), |offset| self.index + offset);
        self.context = self.records.get(self.end).or(self.pending);
        self.anchor = self.records[self.index..self.end]
            .iter()
            .rposition(|r| matches!(r, WalRecord::SegmentBase { .. }))
            .map_or(self.index, |offset| self.index + offset);
        if let Some(WalRecord::LegacyQuantityGridAdopted {
            version,
            sleeves,
            physical,
            ..
        }) = self.context
        {
            if *version != 2 {
                return Err("unsupported legacy quantity adoption version".into());
            }
            let mut owners = BTreeSet::new();
            let mut symbols = BTreeSet::new();
            for row in sleeves {
                if !row.step.is_positive() || !owners.insert((row.strategy, row.symbol)) {
                    return Err("duplicate or invalid legacy sleeve grid context".into());
                }
            }
            for row in physical {
                if !row.step.is_positive() || !symbols.insert(row.symbol) {
                    return Err("duplicate or invalid legacy physical grid context".into());
                }
            }
        } else if self.context.is_some() {
            return Err("expected legacy quantity adoption context".into());
        }
        self.raw = self.normalized.replay_clone()?;
        self.used.clear();
        Ok(())
    }

    fn cut(&mut self, symbol: Option<SymbolId>, terminal: bool) -> Result<(), String> {
        let Some(WalRecord::LegacyQuantityGridAdopted {
            sleeves, physical, ..
        }) = self.context
        else {
            return Ok(());
        };
        let selected = |owner: &Owner| symbol.is_none_or(|symbol| owner.1 == symbol);
        for owner in self
            .raw
            .legacy_quantities
            .keys()
            .filter(|owner| selected(owner))
        {
            self.used.insert(*owner);
            if !sleeves
                .iter()
                .any(|row| (row.strategy, row.symbol) == *owner)
            {
                return Err("missing legacy sleeve grid context".into());
            }
        }
        let mut corrections = Vec::new();
        for (owner, origin) in self
            .normalized
            .legacy_quantities
            .iter()
            .filter(|(owner, _)| selected(owner))
        {
            self.used.insert(*owner);
            let context = sleeves
                .iter()
                .find(|row| (row.strategy, row.symbol) == *owner)
                .ok_or("missing legacy sleeve grid context")?;
            let before = self.normalized.signed_exact(owner.0, owner.1);
            corrections.push(LegacySleeveQuantityCorrection {
                strategy: owner.0,
                symbol: owner.1,
                after: origin.resolve(&before, &context.step)?,
                before,
                step: context.step.clone(),
            });
        }
        self.normalized.adopt_legacy_quantity_subset(&corrections)?;
        let validate_terminal = !(self.planning && self.end == self.records.len());
        if terminal {
            if self.used
                != sleeves
                    .iter()
                    .map(|row| (row.strategy, row.symbol))
                    .collect()
            {
                return Err("legacy sleeve context changes its eligible owner set".into());
            }
            if validate_terminal {
                for row in sleeves {
                    if row.before != self.raw.signed_exact(row.strategy, row.symbol)
                        || row.after != self.normalized.signed_exact(row.strategy, row.symbol)
                    {
                        return Err("legacy sleeve context changes its terminal quantities".into());
                    }
                }
            }
        }
        self.queued.push_back(Event::Cut {
            sleeves: corrections,
            physical,
            symbol,
            terminal,
            validate_terminal,
        });
        Ok(())
    }

    fn rederived(
        &self,
        record: &'a WalRecord,
        dependent: bool,
    ) -> Result<Option<Record<'a>>, String> {
        if !dependent {
            return Ok(Some(Record::Borrowed(record)));
        }
        if let WalRecord::PortfolioOffsetSettled { settlement } = record {
            let mut next = settlement.clone();
            next.slices = self
                .normalized
                .positions_on_symbol(settlement.symbol)
                .map(|row| {
                    let prior = settlement
                        .slices
                        .iter()
                        .find(|slice| slice.strategy == row.strategy)
                        .ok_or("legacy internal correction changes owner keys")?;
                    Ok(engine_types::portfolio_control::PortfolioOffsetSlice {
                        strategy: row.strategy,
                        signed_quantity: -&row.signed_qty,
                        settlement_asset: prior.settlement_asset.clone(),
                    })
                })
                .collect::<Result<Vec<_>, String>>()?;
            if next.slices.is_empty() {
                return Ok(None);
            }
            crate::portfolio_control::validate_settlement(&next)?;
            return Ok(Some(Record::Owned(
                record,
                Box::new(WalRecord::PortfolioOffsetSettled { settlement: next }),
            )));
        }
        use engine_types::execution_allocation::AllocationPolicy;
        let (client, allocation) = match record {
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        client_order_id,
                        allocation: Some(allocation),
                        ..
                    },
                ..
            }
            | WalRecord::RecoveredFill {
                client_order_id,
                allocation: Some(allocation),
                ..
            } => (client_order_id, allocation),
            _ => return Ok(Some(Record::Borrowed(record))),
        };
        if allocation.policy != AllocationPolicy::EmergencyNetFifo || self.context.is_none() {
            return Ok(Some(Record::Borrowed(record)));
        }
        let request = self.sender.get(client.as_str()).copied();
        let symbol = canonical_symbol(record).ok_or("FIFO record has no symbol")?;
        let legacy_step = match self.context {
            Some(WalRecord::LegacyQuantityGridAdopted { sleeves, .. }) => {
                let mut rows = sleeves.iter().filter(|row| row.symbol == symbol);
                let step = rows.next().map(|row| &row.step);
                if rows.any(|row| Some(&row.step) != step) {
                    return Err("legacy FIFO owners disagree on the quantity grid".into());
                }
                step
            }
            _ => None,
        };
        let mut next = record.clone();
        let prepared = match &mut next {
            WalRecord::OrderUpdate { update, .. } => {
                if let OrderUpdate::Fill { allocation, .. } = update {
                    *allocation = None;
                }
                self.normalized.prepare_portfolio_update_on_grid(
                    request,
                    &self.names,
                    update,
                    legacy_step,
                )?
            }
            WalRecord::RecoveredFill { allocation, .. } => {
                *allocation = None;
                self.normalized.prepare_portfolio_recovered_on_grid(
                    request,
                    &self.names,
                    &next,
                    legacy_step,
                )?
            }
            _ => unreachable!(),
        }
        .ok_or("legacy FIFO rederivation has no owned execution")?;
        match &mut next {
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill { allocation, .. },
                ..
            }
            | WalRecord::RecoveredFill { allocation, .. } => {
                *allocation = Some(Box::new(prepared.allocation))
            }
            _ => unreachable!(),
        }
        Ok(Some(Record::Owned(record, Box::new(next))))
    }

    pub(crate) fn next(&mut self) -> Result<Option<Event<'a>>, String> {
        loop {
            if let Some(event) = self.queued.pop_front() {
                return Ok(Some(event));
            }
            if self.done {
                return Ok(None);
            }
            if self.index == self.end && self.context.is_some() {
                self.cut(None, true)?;
                let record = self.context.unwrap();
                self.queued
                    .push_back(Event::Record(Record::Borrowed(record)));
                self.discovered.clear();
                if self.end == self.records.len() {
                    self.done = true;
                } else {
                    self.index += 1;
                    self.span()?;
                }
                return Ok(self.queued.pop_front());
            }
            let Some(record) = self.records.get(self.index) else {
                self.discovered
                    .extend(self.normalized.legacy_quantities.keys().copied());
                self.done = true;
                return Ok(None);
            };
            match record {
                WalRecord::IdentityState { state, .. } => {
                    self.names = state
                        .sleeves
                        .iter()
                        .map(|key| key.as_str().to_owned())
                        .collect()
                }
                WalRecord::Retained(engine_types::wal::RetainedWalRecord::Names {
                    strategies,
                    ..
                }) => self.names = strategies.clone(),
                WalRecord::OrderSent { request, .. } => {
                    self.sender.insert(&request.client_order_id, request);
                }
                WalRecord::OrderLineageRestored { order } => {
                    self.sender
                        .insert(&order.request.client_order_id, &order.request);
                }
                WalRecord::SegmentBase {
                    strategies,
                    open_orders,
                    ..
                } => {
                    self.names = strategies.clone();
                    self.discovered.clear();
                    for order in open_orders {
                        self.sender
                            .insert(&order.request.client_order_id, &order.request);
                    }
                }
                _ => {}
            }
            if let Some(symbol) = canonical_symbol(record) {
                self.discovered.extend(
                    self.normalized
                        .legacy_quantities
                        .keys()
                        .filter(|(_, known)| *known == symbol)
                        .copied(),
                );
                if self.context.is_some() && self.index >= self.anchor {
                    self.cut(Some(symbol), false)?;
                }
            }
            let symbol = canonical_symbol(record).or(match record {
                WalRecord::PortfolioOffsetSettled { settlement } => Some(settlement.symbol),
                _ => None,
            });
            let dependent = self.context.is_some()
                && symbol.is_some_and(|symbol| {
                    !self
                        .raw
                        .positions_on_symbol(symbol)
                        .map(|row| (row.strategy, &row.signed_qty))
                        .eq(self
                            .normalized
                            .positions_on_symbol(symbol)
                            .map(|row| (row.strategy, &row.signed_qty)))
                });
            if self.context.is_some() {
                self.raw.apply_record(record, &self.sender, &self.names)?;
            }
            if let Some(normalized_record) = self.rederived(record, dependent)? {
                self.normalized.apply_record(
                    normalized_record.as_ref(),
                    &self.sender,
                    &self.names,
                )?;
                self.queued.push_back(Event::Record(normalized_record));
            }
            self.index += 1;
            if let Some(event) = self.queued.pop_front() {
                return Ok(Some(event));
            }
        }
    }

    pub(crate) fn finish(mut self) -> Result<Attribution, String> {
        while self.next()?.is_some() {}
        Ok(self.normalized)
    }

    pub(crate) fn state(&self) -> &Attribution {
        &self.normalized
    }
}

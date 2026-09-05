use serde::{Deserialize, Serialize};

use crate::{Intent, OrderRequest};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum OrderDispatchPhase {
    Queued,
    Attempted,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OrderDispatchState {
    pub request: OrderRequest,
    pub intent: Intent,
    pub phase: OrderDispatchPhase,
    pub origin_ns: u64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct QueuedOrderDispatch {
    pub intent: Intent,
    pub origin_ns: u64,
}

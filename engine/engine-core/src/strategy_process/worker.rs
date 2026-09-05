use std::io::{self, Read, Write};
use std::panic::{catch_unwind, AssertUnwindSafe};

use engine_types::strategy_process::{
    CallbackReply, CallbackRequest, SnapshotCtx, MAX_PROCESS_PROPOSAL_BYTES,
    STRATEGY_PROCESS_SCHEMA,
};
use engine_types::EngineEvent;

use super::wire::{read_record, write_record, Budget};

pub fn run_stdio() -> Result<(), String> {
    std::panic::set_hook(Box::new(|_| {}));
    serve(&mut io::stdin().lock(), &mut io::stdout().lock())
}

pub fn serve<R: Read, W: Write>(source: &mut R, output: &mut W) -> Result<(), String> {
    loop {
        let request: CallbackRequest =
            match read_record(source, &mut Budget::new(MAX_PROCESS_PROPOSAL_BYTES)) {
                Ok(request) => request,
                Err(error) if error.kind() == io::ErrorKind::UnexpectedEof => return Ok(()),
                Err(error) => return Err(error.to_string()),
            };
        let mut budget = Budget::new(MAX_PROCESS_PROPOSAL_BYTES);
        let mut output_failed = false;
        let result = catch_unwind(AssertUnwindSafe(|| -> Result<_, String> {
            if request.schema_version != STRATEGY_PROCESS_SCHEMA {
                return Err("unsupported strategy callback schema".into());
            }
            let mut strategy = engine_strategies::runtime::restore(&request.state)?;
            let event = EngineEvent::try_from(&request.event)?;
            let mut context = SnapshotCtx::new(&request.snapshot, |reply| {
                if write_record(output, &reply, &mut budget).is_err() {
                    output_failed = true;
                    std::panic::panic_any("strategy proposal write failed");
                }
            })?;
            strategy.on_event(&event, &mut context);
            drop(context);
            let state = strategy
                .runtime_state()?
                .ok_or_else(|| "registered strategy omitted runtime state".to_string())?;
            Ok((state, strategy.retained_signal_subscriptions()))
        }));
        if output_failed {
            return Err("strategy proposal stream failed before commit".into());
        }
        let reply = match result {
            Ok(Ok((state, retained_signal_subscriptions))) => CallbackReply::Finished {
                callback_id: request.callback_id,
                state,
                retained_signal_subscriptions,
            },
            Ok(Err(reason)) => CallbackReply::Aborted {
                callback_id: request.callback_id,
                reason,
            },
            Err(_) => CallbackReply::Aborted {
                callback_id: request.callback_id,
                reason: "strategy callback panicked or exceeded its output budget".into(),
            },
        };
        // A failed stream may end inside a record; it must never be followed
        // by bytes that could be mistaken for the rest of that record.
        write_record(output, &reply, &mut budget).map_err(|error| error.to_string())?;
    }
}

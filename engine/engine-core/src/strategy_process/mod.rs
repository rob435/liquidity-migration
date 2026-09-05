//! Supervised native callbacks. Only the engine commits their proposals.

use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, SyncSender};
use std::time::Duration;

use engine_types::strategy_process::{
    CallbackReply, CallbackRequest, StrategyRuntimeState, StrategyTimerState,
    MAX_PROCESS_PROPOSAL_BYTES,
};
use engine_types::Action;

pub mod host;
pub mod limits;
mod snapshot;
pub mod state;
pub mod wire;
pub mod worker;

pub const CALLBACK_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug)]
pub struct CallbackProposal {
    pub callback_id: u64,
    pub actions: Vec<Action>,
    pub timers: Vec<StrategyTimerState>,
    pub state: StrategyRuntimeState,
    pub retained_signal_subscriptions: Option<Vec<engine_types::Subscription>>,
}

type Work = (
    CallbackRequest,
    tokio::sync::oneshot::Sender<Result<CallbackProposal, String>>,
);

pub struct StrategyProcess {
    child: Child,
    requests: Option<SyncSender<Work>>,
    io: Option<std::thread::JoinHandle<()>>,
}

impl StrategyProcess {
    pub fn spawn(executable: &Path) -> Result<Self, String> {
        let mut command = Command::new(executable);
        command
            .arg("--strategy-worker")
            .env_clear()
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        Self::spawn_command(command)
    }

    pub fn spawn_command(mut command: Command) -> Result<Self, String> {
        command.stdin(Stdio::piped()).stdout(Stdio::piped());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        limits::install(&mut command);
        let mut child = command.spawn().map_err(|error| error.to_string())?;
        let mut source = child
            .stdout
            .take()
            .ok_or("strategy worker stdout missing")?;
        let mut destination = child.stdin.take().ok_or("strategy worker stdin missing")?;
        let (send, receive) = mpsc::sync_channel::<Work>(1);
        let io = std::thread::Builder::new()
            .name("strategy-process-io".into())
            .spawn(move || {
                while let Ok((request, reply)) = receive.recv() {
                    let result = (|| {
                        wire::write_record(
                            &mut destination,
                            &request,
                            &mut wire::Budget::new(MAX_PROCESS_PROPOSAL_BYTES),
                        )
                        .map_err(|error| error.to_string())?;
                        let mut budget = wire::Budget::new(MAX_PROCESS_PROPOSAL_BYTES);
                        let mut actions = Vec::new();
                        let mut timers = Vec::new();
                        loop {
                            let message: CallbackReply =
                                wire::read_record(&mut source, &mut budget)
                                    .map_err(|error| error.to_string())?;
                            match message {
                                CallbackReply::Action { action } => actions.push(action),
                                CallbackReply::Timer { timer } => timers.push(timer),
                                CallbackReply::Finished {
                                    callback_id,
                                    state,
                                    retained_signal_subscriptions,
                                } => {
                                    if callback_id != request.callback_id {
                                        return Err(
                                            "strategy reply belongs to another callback".into()
                                        );
                                    }
                                    state.validate()?;
                                    if state.kind != request.state.kind
                                        || state.configuration_sha256
                                            != request.state.configuration_sha256
                                    {
                                        return Err(
                                            "strategy reply changed its registered kind".into()
                                        );
                                    }
                                    return Ok(CallbackProposal {
                                        callback_id,
                                        actions,
                                        timers,
                                        state,
                                        retained_signal_subscriptions,
                                    });
                                }
                                CallbackReply::Aborted {
                                    callback_id,
                                    reason,
                                } => {
                                    return Err(format!(
                                        "strategy callback {callback_id} aborted: {reason}"
                                    ));
                                }
                            }
                        }
                    })();
                    let failed = result.is_err();
                    let _ = reply.send(result);
                    if failed {
                        break;
                    }
                }
            })
            .map_err(|error| {
                let _ = child.kill();
                let _ = child.wait();
                error.to_string()
            })?;
        Ok(Self {
            child,
            requests: Some(send),
            io: Some(io),
        })
    }

    pub fn id(&self) -> u32 {
        self.child.id()
    }

    pub async fn call(
        mut self,
        request: CallbackRequest,
        timeout: Duration,
    ) -> Result<(Self, CallbackProposal), String> {
        let (send, receive) = tokio::sync::oneshot::channel();
        self.requests
            .as_ref()
            .ok_or("strategy process is stopped")?
            .try_send((request, send))
            .map_err(|error| error.to_string())?;
        let result = tokio::time::timeout(timeout, receive).await;
        match result {
            Ok(Ok(Ok(proposal))) => Ok((self, proposal)),
            result => {
                self.stop();
                match result {
                    Ok(Ok(Err(error))) => Err(error),
                    Ok(Err(_)) => {
                        Err("strategy process stopped before finishing its callback".into())
                    }
                    Err(_) => {
                        Err("strategy callback exceeded its deadline; process terminated".into())
                    }
                    Ok(Ok(Ok(_))) => unreachable!(),
                }
            }
        }
    }

    pub fn stop(&mut self) {
        self.requests.take();
        #[cfg(unix)]
        if let Ok(group) = i32::try_from(self.child.id()) {
            // The child creates this process group before exec; descendants
            // retaining a pipe must leave with their callback owner.
            unsafe {
                libc::kill(-group, libc::SIGKILL);
            }
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(io) = self.io.take() {
            let _ = io.join();
        }
    }
}

impl Drop for StrategyProcess {
    fn drop(&mut self) {
        self.stop();
    }
}

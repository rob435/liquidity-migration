//! Reproducible offline account-state scaling probe.
//!
//! Run from `engine/`:
//! `cargo run -p engine-core --release --example account_state_soak -- --operations 2000000 --live-ids 65536 --sample-ops 4096 --history-rows 0,1000,10000,100000 --repeats 3`

use std::error::Error;

use engine_core::account_state_bench::{self, Options};

const USAGE: &str = "\
account_state_soak -- offline engine account-state scaling probe

  --operations N       steady-state new executions (default 2000000, max 5000000)
  --live-ids N         fixed retained execution-id set (default 65536, max 1048576)
  --sample-ops N       executions per clock sample (default 4096)
  --history-rows LIST  decoded history tiers, comma-separated (default 0,1000,10000,100000)
  --repeats N          measured cold boots per tier (default 3, max 31)
  --json               emit structured JSON instead of the table

Network, venue JSON parsing, and durable log writes are intentionally absent.
";

fn main() -> Result<(), Box<dyn Error>> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.iter().any(|arg| arg == "-h" || arg == "--help") {
        print!("{USAGE}");
        return Ok(());
    }
    let (options, json) = parse(&args)?;
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let result = runtime.block_on(account_state_bench::run(&options))?;
    if json {
        println!("{}", serde_json::to_string_pretty(&result)?);
    } else {
        print!("{}", result.table());
    }
    Ok(())
}

fn parse(args: &[String]) -> Result<(Options, bool), Box<dyn Error>> {
    let mut options = Options::default();
    let mut json = false;
    let mut at = 0;
    while at < args.len() {
        match args[at].as_str() {
            "--json" => {
                json = true;
                at += 1;
            }
            "--operations" => {
                options.operations = next(args, at)?.parse()?;
                at += 2;
            }
            "--live-ids" => {
                options.live_ids = next(args, at)?.parse()?;
                at += 2;
            }
            "--sample-ops" => {
                options.sample_ops = next(args, at)?.parse()?;
                at += 2;
            }
            "--history-rows" => {
                options.history_rows = next(args, at)?
                    .split(',')
                    .map(str::parse)
                    .collect::<Result<Vec<_>, _>>()?;
                at += 2;
            }
            "--repeats" => {
                options.repeats = next(args, at)?.parse()?;
                at += 2;
            }
            flag => return Err(format!("unknown option {flag}\n\n{USAGE}").into()),
        }
    }
    Ok((options, json))
}

fn next(args: &[String], at: usize) -> Result<&str, Box<dyn Error>> {
    args.get(at + 1)
        .map(String::as_str)
        .ok_or_else(|| format!("{} needs a value", args[at]).into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_line_sets_every_workload_dimension() {
        let args = [
            "--operations",
            "96",
            "--live-ids",
            "8",
            "--sample-ops",
            "4",
            "--history-rows",
            "0,16",
            "--repeats",
            "2",
            "--json",
        ]
        .map(str::to_string);
        let (options, json) = parse(&args).unwrap();
        assert_eq!(options.operations, 96);
        assert_eq!(options.live_ids, 8);
        assert_eq!(options.sample_ops, 4);
        assert_eq!(options.history_rows, vec![0, 16]);
        assert_eq!(options.repeats, 2);
        assert!(json);
    }
}

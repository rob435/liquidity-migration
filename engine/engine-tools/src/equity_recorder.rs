//! Minute observations from fleet artifacts, with local history before an optional metrics push.

use std::collections::BTreeMap;
use std::error::Error;
use std::fmt::Write as _;
use std::fs::{self, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use engine_public::http::HttpClient;
use serde_json::{json, Value};

type Result<T> = std::result::Result<T, Box<dyn Error>>;
const MAX_LINE_BYTES: usize = 4096;
const HEARTBEAT_MAX_AGE_MS: i64 = 60_000;
const RECORDER_MAX_AGE_MS: i64 = 120_000;

pub const ORDER_PATH_FIELDS: &[&str] = &[
    "decide_p50_ns",
    "decide_p99_ns",
    "decide_p999_ns",
    "durable_p50_ns",
    "durable_p99_ns",
    "durable_p999_ns",
    "wire_p50_ns",
    "wire_p99_ns",
    "wire_p999_ns",
    "ack_p50_ns",
    "ack_p99_ns",
    "ack_p999_ns",
    "dispatch_queue_p50_ns",
    "dispatch_queue_p99_ns",
    "dispatch_queue_p999_ns",
    "venue_task_p50_ns",
    "venue_task_p99_ns",
    "venue_task_p999_ns",
    "core_resume_p50_ns",
    "core_resume_p99_ns",
    "core_resume_p999_ns",
    "end_to_end_p50_ns",
    "end_to_end_p99_ns",
    "end_to_end_p999_ns",
    "barrier_wait_p99_ns",
    "barrier_wait_p999_ns",
    "quota_hold_p99_ns",
    "quota_hold_p999_ns",
];

#[derive(Debug, PartialEq)]
struct Source {
    realm: String,
    kind: &'static str,
    path: PathBuf,
}

fn read_sources(path: &Path) -> Result<Vec<Source>> {
    let mut sources = Vec::new();
    for raw in fs::read_to_string(path)?.lines() {
        let line = raw.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let fields: Vec<_> = line.split('|').collect();
        if fields.len() != 16 {
            return Err(format!("fleet manifest row has {} fields: {line:?}", fields.len()).into());
        }
        let (unit, realm, artifact) = (fields[0], fields[2], fields[9]);
        if artifact == "-" {
            continue;
        }
        let (kind, realm) = if unit.starts_with("liquidity-migration-engine") {
            ("engine", realm)
        } else if unit.starts_with("liquidity-migration-signal-worker") {
            ("worker", realm)
        } else if let Some(name) = unit.strip_prefix("liquidity-migration-forward-capture") {
            let name = name
                .strip_suffix(".service")
                .unwrap_or(name)
                .trim_start_matches('-');
            ("recorder", if name.is_empty() { "bybit" } else { name })
        } else {
            continue;
        };
        sources.push(Source {
            realm: realm.into(),
            kind,
            path: artifact.into(),
        });
    }
    if sources.is_empty() {
        return Err(format!("fleet manifest names no artifacts: {}", path.display()).into());
    }
    Ok(sources)
}

fn number(value: &Value) -> Option<f64> {
    value
        .as_bool()
        .map(|b| if b { 1.0 } else { 0.0 })
        .or_else(|| value.as_f64())
}
fn count(value: &Value) -> usize {
    match value {
        Value::Array(a) => a.len(),
        Value::Object(o) => o.len(),
        _ => 0,
    }
}
fn array(value: &Value) -> &[Value] {
    value.as_array().map(Vec::as_slice).unwrap_or(&[])
}
fn rounded(value: f64, digits: usize) -> f64 {
    // Fixed formatting rounds the binary value, including ties such as 2.675.
    format!("{value:.digits$}")
        .parse()
        .expect("formatted float")
}
fn rounded_age(value: Option<f64>) -> Value {
    value
        .map(f64::round_ties_even)
        .filter(|v| *v >= i64::MIN as f64 && *v < -(i64::MIN as f64))
        .map(|v| json!(v as i64))
        .unwrap_or(Value::Null)
}
fn age(now: i64, value: &Value) -> Value {
    json!(number(value).map(|v| (now as f64 - v).max(0.0)))
}
fn ratio(part: &Value, whole: &Value) -> Value {
    json!(number(part)
        .zip(number(whole))
        .and_then(|(p, w)| (w > 0.0).then(|| rounded(p / w, 6))))
}
fn sleeve(value: &Value) -> &str {
    value
        .as_str()
        .filter(|s| !s.is_empty())
        .unwrap_or("unattributed")
}

fn read_sample(source: &Source, clock: &mut impl FnMut() -> i64) -> Value {
    let raw = fs::read_to_string(&source.path);
    // Each source is observed after its read, not at the beginning of the batch.
    let now = clock();
    let mut sample = json!({"ts_ms":now,"realm":source.realm,"kind":source.kind});
    let raw = match raw {
        Ok(raw) => raw,
        Err(error) => {
            sample["state"] = json!(if error.kind() == std::io::ErrorKind::NotFound {
                "absent"
            } else if error.kind() == std::io::ErrorKind::InvalidData && source.kind == "engine" {
                "unparsable"
            } else {
                "unreadable"
            });
            if source.kind == "recorder" || error.kind() != std::io::ErrorKind::NotFound {
                sample["error"] = json!(format!("{}: {}", source.path.display(), error));
            }
            return sample;
        }
    };
    let payload: Value = match serde_json::from_str(&raw) {
        Ok(payload) => payload,
        Err(error) => {
            // Non-standard NaN/Infinity tokens are invalid JSON, including in Python recorder status.
            sample["state"] = json!(if source.kind == "engine" {
                "unparsable"
            } else {
                "unreadable"
            });
            sample["error"] = json!(error.to_string());
            return sample;
        }
    };
    if !payload.is_object() {
        sample["state"] = json!(if source.kind == "engine" {
            "unparsable"
        } else {
            "unreadable"
        });
        sample["error"] = json!(if source.kind == "recorder" {
            "status is not an object"
        } else {
            "heartbeat is not an object"
        });
        return sample;
    }
    let (key, units, limit, age_key) = match source.kind {
        "engine" => ("wall_ts_ms", 1, HEARTBEAT_MAX_AGE_MS, "heartbeat_age_ms"),
        "worker" => ("updated_at_ms", 1, HEARTBEAT_MAX_AGE_MS, "heartbeat_age_ms"),
        _ => (
            "recorded_at_ns",
            1_000_000,
            RECORDER_MAX_AGE_MS,
            "status_age_ms",
        ),
    };
    let stamp = &payload[key];
    // Integer nanoseconds must be divided before conversion to binary64.
    let millis = stamp
        .as_u64()
        .map(|v| (v / units) as f64)
        .or_else(|| stamp.as_f64().map(|v| (v / units as f64).floor()));
    if stamp.is_boolean()
        || number(stamp).is_none_or(|v| v <= 0.0 || !v.is_finite())
        || millis.is_none_or(|v| v > now as f64)
    {
        sample["state"] = json!("unreadable");
        sample["error"] = json!("source timestamp is missing, invalid, or in the future");
        return sample;
    }
    let elapsed = now as f64 - millis.expect("validated timestamp");
    if elapsed > limit as f64 {
        sample["state"] = json!("stale");
        sample[age_key] = rounded_age(Some(elapsed));
        return sample;
    }
    sample["state"] = json!("live");
    match source.kind {
        "engine" => engine_fields(&mut sample, &payload, now),
        "worker" => worker_fields(&mut sample, &payload, now),
        _ => recorder_fields(&mut sample, &payload, now),
    }
    sample
}

fn engine_fields(out: &mut Value, beat: &Value, now: i64) {
    let configured = || {
        array(&beat["strategies"])
            .iter()
            .filter_map(Value::as_str)
            .filter(|s| !s.is_empty())
            .map(|s| (s.to_owned(), 0usize))
            .collect::<BTreeMap<_, _>>()
    };
    let mut sleeves = configured();
    let mut blockers = configured();
    let mut entries = BTreeMap::new();
    let positions = array(&beat["positions"]);
    let mut notional = 0.0;
    for row in positions.iter().filter(|v| v.is_object()) {
        notional +=
            (number(&row["qty"]).unwrap_or(0.0) * number(&row["entry_px"]).unwrap_or(0.0)).abs();
        *sleeves
            .entry(sleeve(&row["strategy"]).to_owned())
            .or_default() += 1;
    }
    for row in array(&beat["strategy_entries_enabled"])
        .iter()
        .filter(|v| v.is_object())
    {
        if let Some(name) = row["strategy"].as_str().filter(|s| !s.is_empty()) {
            entries.insert(name, usize::from(row["entries_enabled"] == true));
        }
    }
    for row in array(&beat["entry_blockers"])
        .iter()
        .filter(|v| v.is_object())
    {
        *blockers
            .entry(sleeve(&row["strategy"]).to_owned())
            .or_default() += 1;
    }
    for key in ["venue", "mode", "engine_commit", "account_user_id"] {
        out[key] = beat[key].clone();
    }
    out["heartbeat_age_ms"] = rounded_age(number(&beat["wall_ts_ms"]).map(|v| now as f64 - v));
    out["account_age_ms"] =
        rounded_age(number(&beat["account_observed_wall_ts_ms"]).map(|v| now as f64 - v));
    out["equity_usdt"] = json!(number(&beat["account_equity_usdt"]));
    out["available_usdt"] = json!(number(&beat["account_available_usdt"]));
    out["position_count"] = json!(positions.len());
    out["position_entry_notional_usdt"] = json!(rounded(notional, 8));
    out["sleeve_positions"] = json!(sleeves);
    out["sleeve_entries_enabled"] = json!(entries);
    out["sleeve_blockers"] = json!(blockers);
    for key in [
        "entry_blockers",
        "strategy_errors",
        "working_entries",
        "pending_flatten_requests",
    ] {
        out[key] = json!(count(&beat[key]));
    }
    for key in [
        "may_open",
        // Recorded beside the latch because `may_open` alone no longer says
        // whether entries were actually being admitted at the sample.
        "private_stream_ready",
        "private_stream_unready_ms",
        "rolling_loss_net_usdt",
        "rolling_loss_limit_usdt",
        "rolling_loss_tripped",
        "rolling_loss_trades",
        "uptime_s",
        "market_events",
        "orders_sent",
        "fills",
        "stream_resets",
        "amends_confirmed",
        "amends_pulled_unconfirmed",
        "venue_clock_offset_ms",
        "fills_maker_share",
        "fill_all_in_arrival_bps",
        "fill_arrival_shortfall_bps",
        "fill_fee_coverage",
        "fill_markout_1m_our_way_bps",
    ]
    .iter()
    .chain(ORDER_PATH_FIELDS)
    {
        out[*key] = json!(number(&beat[*key]));
    }
}

fn worker_fields(out: &mut Value, beat: &Value, now: i64) {
    for key in ["status", "source_generation"] {
        out[key] = beat[key].clone();
    }
    let status = beat["status"].as_str().unwrap_or("");
    out["status_healthy"] = json!(if matches!(status, "starting" | "recovering" | "ready") {
        1.0
    } else {
        0.0
    });
    for name in ["ready", "starting", "recovering"] {
        out[format!("status_{name}")] = json!(if status == name { 1.0 } else { 0.0 });
    }
    for (dst, src) in [
        ("heartbeat_age_ms", "updated_at_ms"),
        ("ws_last_frame_age_ms", "bybit_ws_last_frame_ts_ms"),
        ("long_cycle_age_ms", "last_long_cycle_completed_wall_ts_ms"),
        (
            "carry_cycle_age_ms",
            "last_carry_cycle_completed_wall_ts_ms",
        ),
    ] {
        out[dst] = age(now, &beat[src]);
    }
    out["ws_gap_age_ms"] = if beat["bybit_ws_gap_open"] == true {
        age(now, &beat["bybit_ws_gap_open_since_wall_ts_ms"])
    } else {
        Value::Null
    };
    for (dst, src) in [
        ("ws_connected", "bybit_ws_connected"),
        ("ws_gap_open", "bybit_ws_gap_open"),
        ("ticker_rows", "bybit_ws_ticker_rows"),
        ("ticker_capacity", "bybit_ws_ticker_capacity"),
        (
            "ticker_coverage_complete",
            "bybit_ws_ticker_coverage_complete",
        ),
        ("ticker_topics_accepted", "bybit_ws_ticker_topics_accepted"),
        (
            "ticker_topics_quarantined",
            "bybit_ws_ticker_topics_quarantined",
        ),
        ("kline_topics_accepted", "bybit_ws_kline_topics_accepted"),
        (
            "kline_topics_quarantined",
            "bybit_ws_kline_topics_quarantined",
        ),
    ] {
        out[dst] = json!(number(&beat[src]));
    }
    for key in [
        "rest_ticker_success_count",
        "rest_ticker_failure_count",
        "spool_files",
        "spool_bytes",
        "spool_backpressured",
        "replaceable_outputs_coalesced",
    ] {
        out[key] = json!(number(&beat[key]));
    }
    for (dst, p, w) in [
        (
            "ws_queue_fill",
            "bybit_ws_queued_frames",
            "bybit_ws_queue_capacity",
        ),
        ("spool_file_fill", "spool_files", "spool_file_cap"),
        ("spool_byte_fill", "spool_bytes", "spool_byte_cap"),
    ] {
        out[dst] = ratio(&beat[p], &beat[w]);
    }
    out["spool_backpressured_classes"] = json!(count(&beat["spool_backpressured_classes"]));
}

fn recorder_fields(out: &mut Value, status: &Value, now: i64) {
    let budget = &status["budget"];
    let shards: Vec<_> = array(&status["shards"])
        .iter()
        .filter(|v| v.is_object())
        .collect();
    out["venue"] = status["venue"].clone();
    out["status_age_ms"] =
        rounded_age(number(&status["recorded_at_ns"]).map(|v| now as f64 - v / 1e6));
    out["receive_age_ms"] = rounded_age(
        number(&status["last_receive_ns"])
            .filter(|v| *v != 0.0)
            .map(|v| now as f64 - v / 1e6),
    );
    for (dst, src) in [
        ("projected_month_gb", "projected_month_gb"),
        ("monthly_gb", "monthly_gb"),
        ("budget_over", "over"),
    ] {
        out[dst] = json!(number(&budget[src]));
    }
    out["shed_feeds"] = json!(count(&budget["shed"]));
    for key in [
        "received_frames",
        "written_rows",
        "queued_frames",
        "dropped_frames",
        "disk_dropped_frames",
        "snapshot_failures",
        "free_disk_bytes",
        "queue_capacity",
    ] {
        out[key] = json!(number(&status[key]));
    }
    out["disk_blocked"] = json!(if status["disk_blocked"] == true {
        1.0
    } else {
        0.0
    });
    out["queue_fill"] = ratio(&status["queued_frames"], &status["queue_capacity"]);
    out["shards"] = json!(shards.len());
    out["shards_connected"] = json!(shards.iter().filter(|v| v["connected"] == true).count());
    let reconnects = shards.iter().try_fold(0_i128, |sum, shard| {
        let count = number(&shard["reconnects"]).unwrap_or(0.0);
        if count < i128::MIN as f64 || count >= -(i128::MIN as f64) {
            return None;
        }
        sum.checked_add(count as i128)
    });
    out["reconnects"] = reconnects
        .and_then(serde_json::Number::from_i128)
        .map(Value::Number)
        .unwrap_or(Value::Null);
    out["bytes_24h"] = json!(number(&status["bytes"]["received_24h"]));
}

fn float_text(value: f64) -> String {
    let text = format!("{value:?}");
    if let Some((mantissa, exponent)) = text.split_once('e') {
        let exponent: i32 = exponent.parse().expect("float exponent");
        format!("{mantissa}e{exponent:+03}")
    } else {
        text
    }
}

fn json_text(value: &Value) -> String {
    match value {
        Value::String(_) => {
            let mut text = String::new();
            for c in serde_json::to_string(value).expect("JSON string").chars() {
                if c >= '\u{7f}' {
                    for unit in c.encode_utf16(&mut [0; 2]) {
                        write!(text, "\\u{unit:04x}").unwrap();
                    }
                } else {
                    text.push(c);
                }
            }
            text
        }
        Value::Number(n) if n.is_f64() => float_text(n.as_f64().expect("float number")),
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(json_text).collect::<Vec<_>>().join(",")
        ),
        Value::Object(o) => format!(
            "{{{}}}",
            o.iter()
                .map(|(k, v)| format!("{}:{}", json_text(&json!(k)), json_text(v)))
                .collect::<Vec<_>>()
                .join(",")
        ),
        _ => value.to_string(),
    }
}

fn utc(ts_ms: i64, pattern: &str) -> Result<String> {
    let seconds: libc::time_t = ts_ms.div_euclid(1000);
    let pattern = std::ffi::CString::new(pattern)?;
    let mut tm = std::mem::MaybeUninit::<libc::tm>::uninit();
    let mut buf = [0u8; 64];
    // gmtime_r initializes tm on success and strftime writes within buf.
    let len = unsafe {
        if libc::gmtime_r(&seconds, tm.as_mut_ptr()).is_null() {
            return Err("timestamp is outside UTC calendar range".into());
        }
        libc::strftime(
            buf.as_mut_ptr().cast(),
            buf.len(),
            pattern.as_ptr(),
            tm.as_ptr(),
        )
    };
    if len == 0 {
        return Err("UTC timestamp formatting failed".into());
    }
    Ok(std::str::from_utf8(&buf[..len])?.to_owned())
}

fn sample_path(dir: &Path, sample: &Value) -> Result<PathBuf> {
    Ok(dir.join(format!(
        "{}-{}-{}.jsonl",
        sample["kind"].as_str().ok_or("missing kind")?,
        sample["realm"].as_str().ok_or("missing realm")?,
        utc(
            sample["ts_ms"].as_i64().ok_or("missing timestamp")?,
            "%Y-%m"
        )?
    )))
}
fn append(dir: &Path, sample: &Value) -> Result<PathBuf> {
    let path = sample_path(dir, sample)?;
    let line = json_text(sample) + "\n";
    if line.len() > MAX_LINE_BYTES {
        return Err(format!(
            "sample is {} bytes, over the {MAX_LINE_BYTES} byte append cap",
            line.len()
        )
        .into());
    }
    let mut file = OpenOptions::new().create(true).append(true).open(&path)?;
    file.write_all(line.as_bytes())?;
    Ok(path)
}
fn line_protocol(sample: &Value) -> String {
    let mut fields = BTreeMap::from([(
        "up".to_owned(),
        if sample["state"] == "live" { 1.0 } else { 0.0 },
    )]);
    for (key, value) in sample.as_object().expect("sample object") {
        if matches!(key.as_str(), "ts_ms" | "realm" | "kind" | "state" | "error") {
            continue;
        }
        if let (Some(suffix), Some(sleeves)) = (key.strip_prefix("sleeve_"), value.as_object()) {
            for (name, value) in sleeves {
                if let Some(n) = number(value) {
                    fields.insert(format!("sleeve_{name}_{suffix}"), n);
                }
            }
        } else if let Some(n) = number(value).filter(|n| n.is_finite()) {
            fields.insert(key.clone(), n);
        }
    }
    let realm = sample["realm"]
        .as_str()
        .unwrap()
        .replace(',', "\\,")
        .replace('=', "\\=")
        .replace(' ', "\\ ");
    let fields = fields
        .iter()
        .map(|(k, v)| format!("{k}={}", float_text(*v)))
        .collect::<Vec<_>>()
        .join(",");
    format!(
        "lm_{},realm={realm} {fields} {}",
        sample["kind"].as_str().unwrap(),
        i128::from(sample["ts_ms"].as_i64().unwrap()) * 1_000_000
    )
}

fn cell(value: &Value, digits: usize) -> String {
    number(value)
        .map(|v| format!("{v:.digits$}"))
        .unwrap_or_else(|| "-".into())
}
fn render_curve(dir: &Path, realm: &str, samples: usize) -> Result<String> {
    let empty = || format!("no equity samples yet for {realm} in {}", dir.display());
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(empty()),
        Err(e) => return Err(e.into()),
    };
    let prefix = format!("engine-{realm}-");
    let mut paths = Vec::new();
    for entry in entries {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(&prefix) && name.ends_with(".jsonl") {
            paths.push(entry.path());
        }
    }
    paths.sort();
    let mut rows: Vec<Value> = Vec::new();
    'files: for path in paths.iter().rev() {
        for raw in fs::read_to_string(path)?.lines().rev() {
            if let Ok(row) = serde_json::from_str(raw) {
                rows.push(row);
                if rows.len() >= samples {
                    break 'files;
                }
            }
        }
    }
    rows.reverse();
    if rows.is_empty() {
        return Ok(empty());
    }
    let stamp =
        |row: &Value, fmt| utc(row["ts_ms"].as_i64().ok_or("missing curve timestamp")?, fmt);
    let mut lines = vec![format!(
        "realm={realm} samples={} first={}Z last={}Z",
        rows.len(),
        stamp(&rows[0], "%Y-%m-%d %H:%M")?,
        stamp(rows.last().unwrap(), "%Y-%m-%d %H:%M")?
    )];
    let known: Vec<_> = rows
        .iter()
        .filter_map(|row| number(&row["equity_usdt"]))
        .collect();
    if !known.is_empty() {
        let low = known.iter().copied().reduce(f64::min).unwrap();
        let high = known.iter().copied().reduce(f64::max).unwrap();
        let blocks: Vec<char> = " ▁▂▃▄▅▆▇█".chars().collect();
        let spark: String = rows
            .iter()
            .map(|row| {
                number(&row["equity_usdt"])
                    .map(|v| {
                        if high > low {
                            blocks[1 + ((v - low) / (high - low) * 7.0) as usize]
                        } else {
                            blocks[4]
                        }
                    })
                    .unwrap_or(blocks[0])
            })
            .collect();
        lines.push(format!(
            "equity {low:.2} .. {high:.2} USDT  net {:+.2}",
            known.last().unwrap() - known[0]
        ));
        lines.push(spark);
    }
    let gaps = rows.iter().filter(|r| r["state"] != "live").count();
    if gaps > 0 {
        lines.push(format!(
            "{gaps} of {} samples had no live heartbeat",
            rows.len()
        ));
    }
    lines.push(String::new());
    lines.push(format!(
        "{:<17}{:>11}{:>11}{:>5}{:>6}  state",
        "time", "equity", "avail", "pos", "open?"
    ));
    for row in rows.iter().skip(rows.len().saturating_sub(20)) {
        let open = number(&row["may_open"]).is_some_and(|v| v != 0.0);
        lines.push(format!(
            "{:<17}{:>11}{:>11}{:>5}{:>6}  {}",
            stamp(row, "%m-%d %H:%M")?,
            cell(&row["equity_usdt"], 2),
            cell(&row["available_usdt"], 2),
            cell(&row["position_count"], 0),
            if open { "yes" } else { "no" },
            row["state"].as_str().unwrap_or("None")
        ));
    }
    Ok(lines.join("\n"))
}

#[derive(Debug)]
struct Options {
    state_dir: PathBuf,
    manifest: PathBuf,
    show: Option<String>,
    samples: usize,
}
impl Options {
    fn parse(args: &[String]) -> Result<Self> {
        let mut options = Self {
            state_dir: "/var/lib/liquidity-migration/equity".into(),
            manifest: "/opt/liquidity-migration/deploy/fleet_manifest.tsv".into(),
            show: None,
            samples: 240,
        };
        let mut args = args.iter();
        while let Some(arg) = args.next() {
            let (flag, inline) = arg
                .split_once('=')
                .map_or((arg.as_str(), None), |(flag, value)| (flag, Some(value)));
            let value = inline
                .or_else(|| args.next().map(String::as_str))
                .ok_or_else(|| format!("{flag} needs a value"))?;
            match flag {
                "--state-dir" => options.state_dir = value.into(),
                "--manifest" => options.manifest = value.into(),
                "--show" => options.show = (!value.is_empty()).then(|| value.to_owned()),
                "--samples" => options.samples = value.parse::<i64>()?.max(1) as usize,
                _ => return Err(format!("unknown record-equity option {flag}").into()),
            }
        }
        Ok(options)
    }
}
struct Sink {
    url: String,
    user: String,
    token: String,
}
impl Sink {
    fn from_values(url: &str, user: &str, token: &str) -> Option<Self> {
        let (url, user, token) = (url.trim(), user.trim(), token.trim());
        (!url.is_empty() && !user.is_empty() && !token.is_empty()).then(|| Self {
            url: url.into(),
            user: user.into(),
            token: token.into(),
        })
    }
    async fn push(&self, body: String) -> Result<()> {
        let auth = format!(
            "Basic {}",
            base64::engine::general_purpose::STANDARD
                .encode(format!("{}:{}", self.user, self.token))
        );
        HttpClient::new(&self.url)
            .post_status(
                "",
                body,
                "text/plain; charset=utf-8",
                &[("Authorization", auth)],
            )
            .await?;
        Ok(())
    }
}
struct Output {
    message: String,
    warning: Option<String>,
}
async fn execute(
    options: &Options,
    sink: Option<&Sink>,
    mut clock: impl FnMut() -> i64,
) -> Result<Output> {
    if let Some(realm) = &options.show {
        return Ok(Output {
            message: render_curve(&options.state_dir, realm, options.samples)?,
            warning: None,
        });
    }
    fs::create_dir_all(&options.state_dir)?;
    let mut lines = Vec::new();
    for source in read_sources(&options.manifest)? {
        let sample = read_sample(&source, &mut clock);
        append(&options.state_dir, &sample)?;
        lines.push(line_protocol(&sample));
    }
    let count = lines.len();
    let Some(sink) = sink else {
        return Ok(Output {
            message: format!("recorded {count} samples; no metrics sink configured"),
            warning: None,
        });
    };
    match sink.push(lines.join("\n") + "\n").await {
        Ok(()) => Ok(Output {
            message: format!("recorded and pushed {count} samples"),
            warning: None,
        }),
        Err(e) => Ok(Output {
            message: String::new(),
            warning: Some(format!("WARNING: metrics push failed: {e}")),
        }),
    }
}

pub async fn run(args: &[String]) -> Result<()> {
    if args
        .iter()
        .any(|arg| matches!(arg.as_str(), "-h" | "--help"))
    {
        println!("engine-tools record-equity [--manifest PATH] [--state-dir PATH] [--show REALM] [--samples N]");
        return Ok(());
    }
    let options = Options::parse(args)?;
    let env = |key| std::env::var(key).unwrap_or_default();
    let sink = Sink::from_values(
        &env("METRICS_PUSH_URL"),
        &env("METRICS_PUSH_USER"),
        &env("METRICS_PUSH_TOKEN"),
    );
    let output = execute(&options, sink.as_ref(), || {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock after epoch")
            .as_millis() as i64
    })
    .await?;
    if !output.message.is_empty() {
        println!("{}", output.message);
    }
    if let Some(warning) = output.warning {
        eprintln!("{warning}");
    }
    Ok(())
}

#[cfg(test)]
mod tests;

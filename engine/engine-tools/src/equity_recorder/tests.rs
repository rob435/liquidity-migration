use super::*;
use tempfile::tempdir;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

fn oracle() -> Value {
    serde_json::from_str(include_str!(
        "../../tests/fixtures/equity_recorder_oracle.json"
    ))
    .unwrap()
}
fn source(kind: &'static str, path: &Path) -> Source {
    Source {
        kind,
        realm: "mainnet".into(),
        path: path.into(),
    }
}
fn options(dir: &Path) -> Options {
    Options {
        state_dir: dir.join("equity"),
        manifest: dir.join("manifest"),
        show: None,
        samples: 240,
    }
}
fn row(kind: &str, realm: &str, artifact: &Path) -> String {
    format!("liquidity-migration-{kind}.service|service|{realm}|owner|10|always|direct|-|active|{}|-|-|-|-|-|-\n",artifact.display())
}

#[test]
fn python_oracle_samples_lines_and_months_match() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("input");
    for case in oracle()["cases"].as_array().unwrap() {
        fs::write(&path, case["input"].as_str().unwrap()).unwrap();
        let kind = match case["kind"].as_str().unwrap() {
            "engine" => "engine",
            "worker" => "worker",
            _ => "recorder",
        };
        let source = Source {
            kind,
            realm: case["realm"].as_str().unwrap().into(),
            path: path.clone(),
        };
        let sample = read_sample(&source, &mut || case["now_ms"].as_i64().unwrap());
        assert_eq!(
            json_text(&sample),
            case["sample_json"].as_str().unwrap(),
            "{kind} {}",
            case["name"]
        );
        assert_eq!(
            line_protocol(&sample),
            case["line_protocol"].as_str().unwrap(),
            "{kind} {}",
            case["name"]
        );
        assert_eq!(
            sample_path(dir.path(), &sample)
                .unwrap()
                .file_name()
                .unwrap()
                .to_str()
                .unwrap(),
            case["filename"].as_str().unwrap()
        );
    }
}

#[test]
fn freshness_constants_are_the_shared_liveness_oracle() {
    let limits = &oracle()["freshness_limits_ms"];
    assert_eq!(HEARTBEAT_MAX_AGE_MS, limits["engine"].as_i64().unwrap());
    assert_eq!(HEARTBEAT_MAX_AGE_MS, limits["worker"].as_i64().unwrap());
    assert_eq!(RECORDER_MAX_AGE_MS, limits["recorder"].as_i64().unwrap());
}

#[test]
fn rounding_matches_binary_value_python_results() {
    for case in oracle()["rounding"].as_array().unwrap() {
        let actual = rounded(
            case["value"].as_f64().unwrap(),
            case["digits"].as_u64().unwrap() as usize,
        );
        assert_eq!(
            actual.to_bits(),
            case["expected"].as_f64().unwrap().to_bits(),
            "{case}"
        );
    }
    assert_eq!(float_text(1e-7), "1e-07");
    assert_eq!(float_text(1e20), "1e+20");
    assert_eq!(float_text(-0.0), "-0.0");
    assert_eq!(
        json_text(&json!("démo 😃\u{7f}")),
        r#""d\u00e9mo \ud83d\ude03\u007f""#
    );
}

#[test]
fn manifest_owns_all_ten_sources_and_their_order() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../deploy/fleet_manifest.tsv");
    let sources = read_sources(&path).unwrap();
    assert_eq!(sources.len(), 10);
    assert_eq!(
        sources
            .iter()
            .map(|s| (s.kind, s.realm.as_str()))
            .collect::<Vec<_>>(),
        vec![
            ("recorder", "bybit"),
            ("recorder", "binance"),
            ("worker", "demo"),
            ("worker", "mainnet"),
            ("worker", "mexc"),
            ("worker", "hyperliquid"),
            ("engine", "demo"),
            ("engine", "mainnet"),
            ("engine", "mexc"),
            ("engine", "hyperliquid")
        ]
    );
    assert_eq!(
        sources[7].path,
        Path::new("/var/lib/liquidity-migration-engine-mainnet/heartbeat.json")
    );
    assert_eq!(
        sources[8].path,
        Path::new("/var/lib/liquidity-migration-engine-mexc/heartbeat.json")
    );
    assert_eq!(
        sources[9].path,
        Path::new("/var/lib/liquidity-migration-engine-hyperliquid/heartbeat.json")
    );
    let dir = tempdir().unwrap();
    let bad = dir.path().join("manifest");
    fs::write(&bad, "one|two\n").unwrap();
    assert!(read_sources(&bad)
        .unwrap_err()
        .to_string()
        .contains("2 fields"));
    fs::write(&bad, "# empty\n\n").unwrap();
    assert!(read_sources(&bad)
        .unwrap_err()
        .to_string()
        .contains("no artifacts"));
}

#[test]
fn source_failures_are_down_observations() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("missing");
    for kind in ["engine", "worker", "recorder"] {
        let s = source(kind, &path);
        let sample = read_sample(&s, &mut || 1_788_000_000_000);
        assert_eq!(sample["state"], "absent");
        assert_eq!(sample.get("error").is_some(), kind == "recorder");
        assert_eq!(line_protocol(&sample).split(' ').nth(1), Some("up=0.0"));
        let sample = read_sample(&source(kind, dir.path()), &mut || 1_788_000_000_000);
        assert_eq!(sample["state"], "unreadable");
    }
    for raw in ["{ broken", "[]", "null", "true"] {
        fs::write(&path, raw).unwrap();
        for kind in ["engine", "worker", "recorder"] {
            let sample = read_sample(&source(kind, &path), &mut || 1_788_000_000_000);
            assert_eq!(
                sample["state"],
                if kind == "engine" {
                    "unparsable"
                } else {
                    "unreadable"
                }
            );
            assert!(sample.get("equity_usdt").is_none());
        }
    }
    fs::write(&path, [0xff]).unwrap();
    assert_eq!(
        read_sample(&source("engine", &path), &mut || 1000)["state"],
        "unparsable"
    );
    assert_eq!(
        read_sample(&source("worker", &path), &mut || 1000)["state"],
        "unreadable"
    );
}

#[test]
fn bare_nonfinite_tokens_are_invalid_json_instead_of_python_extensions() {
    // This deliberate boundary differs from Python's json.loads; no invalid token is republished.
    let dir = tempdir().unwrap();
    let path = dir.path().join("input");
    for token in ["NaN", "Infinity", "-Infinity"] {
        for (kind, key) in [
            ("engine", "wall_ts_ms"),
            ("worker", "updated_at_ms"),
            ("recorder", "recorded_at_ns"),
        ] {
            for raw in [
                format!("{{\"{key}\":{token}}}"),
                format!("{{\"{key}\":1788000000000000000,\"budget\":{{\"monthly_gb\":{token}}}}}"),
            ] {
                fs::write(&path, raw).unwrap();
                let sample = read_sample(&source(kind, &path), &mut || 1_788_000_000_000);
                assert_eq!(
                    sample["state"],
                    if kind == "engine" {
                        "unparsable"
                    } else {
                        "unreadable"
                    }
                );
                assert_eq!(line_protocol(&sample).split(' ').nth(1), Some("up=0.0"));
            }
        }
    }
}

#[test]
fn clock_is_observed_after_read_and_each_batch_source_has_its_own_observation() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("input");
    fs::write(&path, r#"{"wall_ts_ms":1000}"#).unwrap();
    let sample = read_sample(&source("engine", &path), &mut || {
        fs::write(&path, "invalid").unwrap();
        1001
    });
    assert_eq!(sample["state"], "live");
    assert_eq!(sample["ts_ms"], 1001);
    assert_eq!(sample["heartbeat_age_ms"], 1);
}

#[test]
fn nanosecond_freshness_keeps_integer_precision_at_the_boundary() {
    let dir = tempdir().unwrap();
    let path = dir.path().join("input");
    let now = 1_788_000_000_000i64;
    fs::write(
        &path,
        format!("{{\"recorded_at_ns\":{}}}", now * 1_000_000 + 999_999),
    )
    .unwrap();
    assert_eq!(
        read_sample(&source("recorder", &path), &mut || now)["state"],
        "live"
    );
    fs::write(
        &path,
        format!("{{\"recorded_at_ns\":{}}}", (now + 1) * 1_000_000),
    )
    .unwrap();
    assert_eq!(
        read_sample(&source("recorder", &path), &mut || now)["state"],
        "unreadable"
    );
}

#[test]
fn append_keeps_bytes_and_refuses_oversize_before_opening() {
    let dir = tempdir().unwrap();
    let mut sample = json!({"ts_ms":1788000000000i64,"kind":"engine","realm":"mainnet","state":"live","text":""});
    let overhead = json_text(&sample).len() + 1;
    sample["text"] = json!("x".repeat(MAX_LINE_BYTES - overhead));
    let path = append(dir.path(), &sample).unwrap();
    assert_eq!(fs::read(&path).unwrap().len(), MAX_LINE_BYTES);
    append(dir.path(), &sample).unwrap();
    assert_eq!(fs::read(&path).unwrap().len(), 2 * MAX_LINE_BYTES);
    sample["text"] = json!("é".repeat(MAX_LINE_BYTES / 6));
    assert!(append(dir.path(), &sample)
        .unwrap_err()
        .to_string()
        .contains("append cap"));
    assert_eq!(fs::read(&path).unwrap().len(), 2 * MAX_LINE_BYTES);
    sample["realm"] = json!("new");
    assert!(append(dir.path(), &sample).is_err());
    assert!(!sample_path(dir.path(), &sample).unwrap().exists());
}

#[test]
fn curve_matches_python_and_reads_older_months_skipping_torn_lines() {
    let dir = tempdir().unwrap();
    let fixture = oracle();
    for row in fixture["curve"]["rows"].as_array().unwrap() {
        append(dir.path(), row).unwrap();
    }
    assert_eq!(
        render_curve(dir.path(), "mainnet", 240).unwrap(),
        fixture["curve"]["text"].as_str().unwrap()
    );
    let last = sample_path(dir.path(), &fixture["curve"]["rows"][0]).unwrap();
    OpenOptions::new()
        .append(true)
        .open(last)
        .unwrap()
        .write_all(b"\n{torn\n")
        .unwrap();
    assert_eq!(
        render_curve(dir.path(), "mainnet", 3)
            .unwrap()
            .lines()
            .next()
            .unwrap()
            .split_whitespace()
            .nth(1),
        Some("samples=3")
    );
    let mut row = fixture["curve"]["rows"][0].clone();
    row["ts_ms"] = json!(1_785_000_000_000i64);
    append(dir.path(), &row).unwrap();
    assert!(render_curve(dir.path(), "mainnet", 240)
        .unwrap()
        .contains("samples=11"));
    assert!(render_curve(&dir.path().join("missing"), "mainnet", 1)
        .unwrap()
        .contains("no equity samples yet"));
}

#[test]
fn options_and_sink_configuration_preserve_defaults_and_partial_disable() {
    let opts = Options::parse(&[]).unwrap();
    assert_eq!(opts.samples, 240);
    let opts = Options::parse(&[
        "--samples".into(),
        "-2".into(),
        "--show".into(),
        "demo".into(),
    ])
    .unwrap();
    assert_eq!(opts.samples, 1);
    assert_eq!(opts.show.as_deref(), Some("demo"));
    assert!(Options::parse(&["--manifest".into()]).is_err());
    assert!(Options::parse(&["--wat".into(), "x".into()]).is_err());
    for (u, n, t) in [
        ("", "user", "token"),
        ("url", " ", "token"),
        ("url", "user", ""),
    ] {
        assert!(Sink::from_values(u, n, t).is_none());
    }
    let sink = Sink::from_values(" url ", " user ", " token ").unwrap();
    assert_eq!(
        (sink.url.as_str(), sink.user.as_str(), sink.token.as_str()),
        ("url", "user", "token")
    );
}

async fn receiver(
    reply: &'static [u8],
    state_dir: PathBuf,
    expected_files: usize,
) -> (String, tokio::task::JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/write", listener.local_addr().unwrap());
    let task = tokio::spawn(async move {
        let (mut socket, _) = listener.accept().await.unwrap();
        let mut data = Vec::new();
        loop {
            let mut buf = [0; 2048];
            let n = socket.read(&mut buf).await.unwrap();
            assert!(n > 0);
            data.extend_from_slice(&buf[..n]);
            let raw = String::from_utf8_lossy(&data);
            if let Some((head, body)) = raw.split_once("\r\n\r\n") {
                let length = head
                    .lines()
                    .find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("content-length: ")
                            .map(|s| s.parse::<usize>().unwrap())
                    })
                    .unwrap();
                if body.len() >= length {
                    break;
                }
            }
        }
        assert_eq!(
            fs::read_dir(&state_dir).unwrap().count(),
            expected_files,
            "all local appends precede HTTP"
        );
        socket.write_all(reply).await.unwrap();
        String::from_utf8(data).unwrap()
    });
    (url, task)
}

#[tokio::test]
async fn no_sink_records_every_source_with_separate_clock_observations() {
    let dir = tempdir().unwrap();
    let options = options(dir.path());
    let input = dir.path().join("beat");
    fs::write(&input, r#"{"wall_ts_ms":1000}"#).unwrap();
    fs::write(
        &options.manifest,
        row("engine", "demo", &input) + &row("engine-mainnet", "mainnet", &input),
    )
    .unwrap();
    let mut now = 1000;
    let output = execute(&options, None, || {
        now += 1;
        now
    })
    .await
    .unwrap();
    assert_eq!(
        output.message,
        "recorded 2 samples; no metrics sink configured"
    );
    assert!(output.warning.is_none());
    let mut stamps = Vec::new();
    for file in fs::read_dir(&options.state_dir).unwrap() {
        let v: Value =
            serde_json::from_str(&fs::read_to_string(file.unwrap().path()).unwrap()).unwrap();
        stamps.push(v["ts_ms"].as_i64().unwrap());
    }
    stamps.sort();
    assert_eq!(stamps, vec![1001, 1002]);
}

#[tokio::test]
async fn show_never_samples_makes_a_directory_or_pushes() {
    let dir = tempdir().unwrap();
    let mut opts = options(dir.path());
    opts.show = Some("mainnet".into());
    let sink = Sink::from_values("http://127.0.0.1:1", "u", "t").unwrap();
    let result = execute(&opts, Some(&sink), || panic!("show must not sample"))
        .await
        .unwrap();
    assert!(result.message.contains("no equity samples yet"));
    assert!(!opts.state_dir.exists());
}

#[tokio::test]
async fn status_only_push_keeps_auth_bytes_body_and_local_first_order() {
    let dir = tempdir().unwrap();
    let opts = options(dir.path());
    let input = dir.path().join("beat");
    fs::write(&input, r#"{"wall_ts_ms":1000}"#).unwrap();
    fs::write(
        &opts.manifest,
        row("engine", "demo", &input) + &row("engine-mainnet", "mainnet", &input),
    )
    .unwrap();
    let (url, server) = receiver(
        b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n",
        opts.state_dir.clone(),
        2,
    )
    .await;
    let sink = Sink::from_values(&url, "u", "t").unwrap();
    let output = execute(&opts, Some(&sink), || 1000).await.unwrap();
    assert_eq!(output.message, "recorded and pushed 2 samples");
    assert!(output.warning.is_none());
    let request = server.await.unwrap();
    let lower = request.to_ascii_lowercase();
    assert!(request.starts_with("POST /write HTTP/1.1\r\n"));
    assert!(lower.contains("content-type: text/plain; charset=utf-8\r\n"));
    assert!(lower.contains("authorization: basic dtp0\r\n"));
    let body = request.split_once("\r\n\r\n").unwrap().1;
    assert!(body.ends_with('\n'));
    assert_eq!(body.lines().count(), 2);
    assert!(body.starts_with("lm_engine,realm=demo "));
}

#[tokio::test]
async fn rejected_or_redirected_push_warns_without_losing_local_samples() {
    for reply in [b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".as_slice(),b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/other\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".as_slice()] {
        let dir=tempdir().unwrap();let opts=options(dir.path());fs::write(&opts.manifest,row("engine","demo",&dir.path().join("absent"))).unwrap();
        let (url,server)=receiver(reply,opts.state_dir.clone(),1).await;let sink=Sink::from_values(&url,"u","t").unwrap();let output=execute(&opts,Some(&sink),||1000).await.unwrap();
        assert!(output.message.is_empty());assert!(output.warning.unwrap().starts_with("WARNING: metrics push failed:"));server.await.unwrap();
    }
}

#[tokio::test]
async fn transport_failure_is_best_effort_but_local_errors_are_fatal() {
    let dir = tempdir().unwrap();
    let opts = options(dir.path());
    fs::write(
        &opts.manifest,
        row("engine", "demo", &dir.path().join("absent")),
    )
    .unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    drop(listener);
    let sink = Sink::from_values(&url, "u", "t").unwrap();
    assert!(execute(&opts, Some(&sink), || 1000)
        .await
        .unwrap()
        .warning
        .is_some());
    fs::write(&opts.manifest, "bad").unwrap();
    assert!(execute(&opts, Some(&sink), || 1000).await.is_err());
    let mut opts = opts;
    opts.state_dir = dir.path().join("file");
    fs::write(&opts.state_dir, "not a directory").unwrap();
    assert!(execute(&opts, None, || 1000).await.is_err());
}

#[test]
fn equals_form_cli_values_match_separate_arguments() {
    let options = Options::parse(&[
        "--samples=1".into(),
        "--show=demo".into(),
        "--state-dir=/tmp/equity".into(),
        "--manifest=/tmp/fleet".into(),
    ])
    .unwrap();
    assert_eq!(options.samples, 1);
    assert_eq!(options.show.as_deref(), Some("demo"));
    assert_eq!(options.state_dir, Path::new("/tmp/equity"));
    assert_eq!(options.manifest, Path::new("/tmp/fleet"));
}

#[tokio::test]
async fn command_help_needs_no_value_or_files() {
    run(&["--help".into()]).await.unwrap();
    run(&["-h".into()]).await.unwrap();
}

#[test]
fn derived_overflow_and_unrepresentable_ages_are_null_not_saturated() {
    let mut sample = json!({});
    engine_fields(
        &mut sample,
        &json!({"wall_ts_ms":1000,"account_observed_wall_ts_ms":-1e308,"positions":[{"qty":1e308,"entry_px":2.0}]}),
        1000,
    );
    assert!(sample["position_entry_notional_usdt"].is_null());
    assert!(sample["account_age_ms"].is_null());
    assert_eq!(rounded_age(Some(i64::MIN as f64)), json!(i64::MIN));
    assert!(rounded_age(Some(-(i64::MIN as f64))).is_null());
}

#[test]
fn reconnect_totals_extend_through_u64_without_overflowing() {
    let mut sample = json!({});
    recorder_fields(
        &mut sample,
        &json!({"recorded_at_ns":1000000000,"shards":[{"reconnects":9_000_000_000_000_000_000u64},{"reconnects":9_000_000_000_000_000_000u64}]}),
        1000,
    );
    assert_eq!(
        sample["reconnects"].as_u64(),
        Some(18_000_000_000_000_000_000)
    );
    recorder_fields(
        &mut sample,
        &json!({"shards":[{"reconnects":1e308},{"reconnects":1e308}]}),
        1000,
    );
    assert!(sample["reconnects"].is_null());
}

fn venue_amount(numerator: &str, denominator: &str) -> Value {
    json!({"value":{"n":numerator,"d":denominator},"provenance":"VenueDecimal"})
}
fn venue_position(side: &str, qty: f64, entry: &str, mark: Option<Value>) -> Value {
    let mut row = json!({
        "symbol": 7,
        "side": side,
        "qty": qty,
        "entry_px": entry.parse::<f64>().unwrap(),
        "stop_attached": false,
        "leverage": 3.0,
        "exact_amounts": {
            "quantity": venue_amount(&format!("{}", (qty * 1000.0).round()), "1000"),
            "entry_price": venue_amount(entry, "1"),
        },
    });
    if let Some(mark) = mark {
        row["exact_amounts"]["mark_price"] = mark;
    }
    row
}
fn engine_beat(venue_positions: Value) -> Value {
    json!({
        "wall_ts_ms": 1000,
        "account_observed_wall_ts_ms": 1000,
        "account_equity_usdt": 130.28,
        "positions": [
            {"symbol":"NEARUSDT","side":"long","qty":20.7,"entry_px":2.0,"strategy":"long"},
            {"symbol":"AAVEUSDT","side":"short","qty":1.0,"entry_px":100.0,"strategy":null},
        ],
        "account_metrics": {
            "equity_usdt": 130.28,
            "available_usdt": 118.29,
            "observed_ns": 1,
            "positions": venue_positions,
        },
    })
}

#[test]
fn the_venue_marks_split_equity_into_wallet_cash_and_unrealised_pnl() {
    let mut sample = json!({});
    engine_fields(
        &mut sample,
        &engine_beat(json!([
            venue_position("Buy", 20.7, "2", Some(venue_amount("5", "2"))),
            venue_position("Sell", 1.0, "100", Some(venue_amount("90", "1"))),
        ])),
        1000,
    );

    // (2.5 - 2) * 20.7 long, plus (100 - 90) * 1 short.
    assert_eq!(sample["unrealised_pnl_usdt"], json!(20.35));
    assert_eq!(sample["wallet_cash_usdt"], json!(109.93));
    assert_eq!(
        sample["positions"],
        json!([
            {"symbol":"NEARUSDT","side":"long","qty":20.7,"entry_px":2.0,"mark_px":2.5,"strategy":"long"},
            {"symbol":"AAVEUSDT","side":"short","qty":1.0,"entry_px":100.0,"mark_px":90.0,"strategy":null},
        ])
    );
    assert_eq!(sample["positions_truncated"], json!(false));
    assert_eq!(sample["position_count"], json!(2));
}

#[test]
fn an_unreadable_mark_leaves_the_sum_null_and_an_ambiguous_join_leaves_the_mark_null() {
    let mut unpriced = json!({});
    engine_fields(
        &mut unpriced,
        &engine_beat(json!([
            venue_position("Buy", 20.7, "2", Some(venue_amount("5", "2"))),
            venue_position("Sell", 1.0, "100", None),
        ])),
        1000,
    );
    // One unreadable row leaves the account's unrealised P&L unknown, not 10.35.
    assert!(unpriced["unrealised_pnl_usdt"].is_null());
    assert!(unpriced["wallet_cash_usdt"].is_null());
    assert_eq!(unpriced["positions"][0]["mark_px"], json!(2.5));
    assert!(unpriced["positions"][1]["mark_px"].is_null());

    let mut ambiguous = json!({});
    engine_fields(
        &mut ambiguous,
        &engine_beat(json!([
            venue_position("Buy", 20.7, "2", Some(venue_amount("5", "2"))),
            venue_position("Buy", 20.7, "2", Some(venue_amount("7", "2"))),
            venue_position("Sell", 1.0, "100", Some(venue_amount("90", "1"))),
        ])),
        1000,
    );
    assert!(ambiguous["positions"][0]["mark_px"].is_null());
    assert_eq!(ambiguous["positions"][1]["mark_px"], json!(90.0));
    // Both Buy rows still count: only the name-to-mark join is ambiguous.
    assert_eq!(ambiguous["unrealised_pnl_usdt"], json!(51.4));

    let mut absent = json!({});
    engine_fields(
        &mut absent,
        &json!({"wall_ts_ms":1000,"positions":[]}),
        1000,
    );
    assert!(absent["unrealised_pnl_usdt"].is_null());
    assert!(absent["wallet_cash_usdt"].is_null());
    assert_eq!(absent["positions"], json!([]));
}

#[test]
fn a_position_list_over_the_append_cap_is_dropped_and_says_so() {
    let holdings: Vec<Value> = (0..60)
        .map(|i| json!({"symbol":format!("SYMBOL{i:03}USDT"),"side":"long","qty":20.7,"entry_px":2.0,"strategy":"long"}))
        .collect();
    let mut sample =
        json!({"ts_ms":1788000000000i64,"kind":"engine","realm":"mainnet","state":"live"});
    engine_fields(
        &mut sample,
        &json!({
            "wall_ts_ms": 1000,
            "account_equity_usdt": 130.28,
            "positions": holdings,
            "account_metrics": {"positions":[venue_position("Buy", 20.7, "2", Some(venue_amount("5","2")))]},
        }),
        1000,
    );

    assert!(sample["positions"].is_null());
    assert_eq!(sample["positions_truncated"], json!(true));
    assert_eq!(sample["unrealised_pnl_usdt"], json!(10.35));
    assert_eq!(sample["wallet_cash_usdt"], json!(119.93));
    assert_eq!(sample["position_count"], json!(60));
    let dir = tempdir().unwrap();
    assert!(json_text(&sample).len() < MAX_LINE_BYTES);
    append(dir.path(), &sample).unwrap();
}

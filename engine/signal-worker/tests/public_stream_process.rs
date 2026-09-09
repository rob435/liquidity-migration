use signal_worker::http::wall_ms;
use signal_worker::venue::bybit::BybitPublicStream;
use std::process::Command;
use std::time::{Duration, Instant};

#[test]
#[ignore = "needs network; runs the public stream in a separate process"]
fn live_public_stream_accepts_btc_topics_and_delivers_a_ticker() {
    const CHILD: &str = "R3_PUBLIC_STREAM_PROCESS";
    if std::env::var_os(CHILD).is_none() {
        let output = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "live_public_stream_accepts_btc_topics_and_delivers_a_ticker",
                "--ignored",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        return;
    }
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
        .block_on(async {
            let stream = BybitPublicStream::spawn(vec!["BTCUSDT".into()], 10_000, 250).unwrap();
            let deadline = Instant::now() + Duration::from_secs(20);
            loop {
                let health = stream.health();
                if health.connected
                    && health.ticker_topics_accepted == 1
                    && health.kline_topics_accepted == 1
                {
                    if let Some(sample) = stream.sample_tickers(wall_ms().unwrap(), 30_000) {
                        if sample.rows.len() == 1 && sample.rows[0].mark_price.is_some() {
                            let complete = stream.health();
                            assert!(complete.ticker_coverage_complete);
                            assert_eq!(complete.ticker_topics_quarantined, 0);
                            assert_eq!(complete.kline_topics_quarantined, 0);
                            return;
                        }
                    }
                }
                assert!(
                    Instant::now() < deadline,
                    "Bybit public worker stream did not become complete: {health:?}"
                );
                tokio::time::sleep(Duration::from_millis(100)).await;
            }
        });
}

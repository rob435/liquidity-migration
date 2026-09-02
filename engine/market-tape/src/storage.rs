use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};

pub fn utc_day_hour(epoch_ns: i64) -> (String, String) {
    let secs = (epoch_ns / 1_000_000_000) as u64;
    // Calculate UTC day and hour
    let days_since_epoch = secs / 86400;
    let seconds_into_day = secs % 86400;
    let hour = (seconds_into_day / 3600) as u32;

    // Convert days_since_epoch to YYYY-MM-DD
    let (year, month, day) = days_to_date(days_since_epoch);
    (
        format!("{:04}-{:02}-{:02}", year, month, day),
        format!("{:02}", hour),
    )
}

fn days_to_date(days: u64) -> (i32, u32, u32) {
    // Standard civil calendar algorithm
    let z = days as i64 + 719468;
    let era = (if z >= 0 { z } else { z - 146096 }) / 146097;
    let doe = (z - era * 146097) as u32;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if m <= 2 { y + 1 } else { y };
    (year as i32, m, d)
}

pub struct ActiveSegment {
    pub symbol: String,
    pub day: String,
    pub hour: String,
    pub index: u32,
    pub partial_path: PathBuf,
    pub writer: BufWriter<File>,
    pub bytes_written: u64,
    pub records: u64,
    pub first_receive_ns: i64,
    pub last_receive_ns: i64,
}

pub struct SegmentWriter {
    root: PathBuf,
    max_bytes: u64,
    active: BTreeMap<String, ActiveSegment>,
    manifest: Arc<Mutex<File>>,
}

impl SegmentWriter {
    pub fn new(root: impl Into<PathBuf>, max_bytes: u64) -> std::io::Result<Self> {
        let root = root.into();
        fs::create_dir_all(&root)?;
        let manifest_path = root.join("manifest.jsonl");
        let manifest_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&manifest_path)?;
        Ok(Self {
            root,
            max_bytes,
            active: BTreeMap::new(),
            manifest: Arc::new(Mutex::new(manifest_file)),
        })
    }

    pub fn write_record(
        &mut self,
        symbol: &str,
        record_bytes: &[u8],
        receive_ns: i64,
    ) -> std::io::Result<()> {
        let (day, hour) = utc_day_hour(receive_ns);

        let needs_rotation = match self.active.get(symbol) {
            Some(seg) => {
                seg.day != day
                    || seg.hour != hour
                    || (seg.bytes_written + record_bytes.len() as u64) > self.max_bytes
            }
            None => false,
        };

        if needs_rotation {
            self.close_segment(symbol)?;
        }

        if !self.active.contains_key(symbol) {
            self.open_segment(symbol, day, hour, receive_ns)?;
        }

        let seg = self.active.get_mut(symbol).unwrap();
        seg.writer.write_all(record_bytes)?;
        seg.writer.write_all(b"\n")?;
        seg.bytes_written += record_bytes.len() as u64 + 1;
        seg.records += 1;
        seg.last_receive_ns = receive_ns;

        Ok(())
    }

    fn open_segment(
        &mut self,
        symbol: &str,
        day: String,
        hour: String,
        receive_ns: i64,
    ) -> std::io::Result<()> {
        let symbol_dir = self.root.join(&day).join(&hour).join(symbol);
        fs::create_dir_all(&symbol_dir)?;

        // Find next segment index
        let mut max_idx = 0;
        if let Ok(entries) = fs::read_dir(&symbol_dir) {
            for entry in entries.flatten() {
                let name = entry.file_name();
                let name_str = name.to_string_lossy();
                if let Some(rest) = name_str.strip_prefix("segment-") {
                    let num_part = rest.split('.').next().unwrap_or("");
                    if let Ok(idx) = num_part.parse::<u32>() {
                        max_idx = max_idx.max(idx + 1);
                    }
                }
            }
        }

        let filename = format!("segment-{:06}.jsonl.partial", max_idx);
        let partial_path = symbol_dir.join(filename);
        let file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&partial_path)?;

        self.active.insert(
            symbol.to_string(),
            ActiveSegment {
                symbol: symbol.to_string(),
                day,
                hour,
                index: max_idx,
                partial_path,
                writer: BufWriter::with_capacity(65536, file),
                bytes_written: 0,
                records: 0,
                first_receive_ns: receive_ns,
                last_receive_ns: receive_ns,
            },
        );
        Ok(())
    }

    pub fn close_segment(&mut self, symbol: &str) -> std::io::Result<()> {
        if let Some(mut seg) = self.active.remove(symbol) {
            seg.writer.flush()?;
            let final_jsonl = seg.partial_path.with_extension("");
            fs::rename(&seg.partial_path, &final_jsonl)?;
            sync_directory(final_jsonl.parent().unwrap())?;

            let root = self.root.clone();
            let manifest = self.manifest.clone();
            let records = seg.records;
            let first_ns = seg.first_receive_ns;
            let last_ns = seg.last_receive_ns;
            let sym = seg.symbol.clone();
            let day = seg.day.clone();
            let hour = seg.hour.clone();

            tokio::task::spawn_blocking(move || {
                let zst_path = final_jsonl.with_extension("jsonl.zst");
                match zstd_compress(&final_jsonl, &zst_path) {
                    Ok(digest) => {
                        let _ = fs::remove_file(&final_jsonl);
                        let _ = sync_directory(zst_path.parent().unwrap());
                        let rel_path = zst_path.strip_prefix(&root).unwrap_or(&zst_path);
                        let size = zst_path.metadata().map(|m| m.len()).unwrap_or(0);
                        let now_ns = SystemTime::now()
                            .duration_since(UNIX_EPOCH)
                            .unwrap()
                            .as_nanos() as i64;

                        let receipt = serde_json::json!({
                            "kind": "segment_compressed",
                            "recorded_at_ns": now_ns,
                            "path": rel_path.to_string_lossy(),
                            "symbol": sym,
                            "day": day,
                            "hour": hour,
                            "records": records,
                            "first_receive_ns": first_ns,
                            "last_receive_ns": last_ns,
                            "compressed_bytes": size,
                            "sha256": digest
                        });
                        let mut line = serde_json::to_vec(&receipt).unwrap();
                        line.push(b'\n');

                        if let Ok(mut handle) = manifest.lock() {
                            let _ = handle.write_all(&line);
                            let _ = handle.flush();
                            let _ = handle.sync_all();
                        }
                    }
                    Err(err) => {
                        tracing::error!(error = %err, path = %final_jsonl.display(), "zstd compression failed");
                    }
                }
            });
        }
        Ok(())
    }

    pub fn flush_all(&mut self) -> std::io::Result<()> {
        let symbols: Vec<String> = self.active.keys().cloned().collect();
        for sym in symbols {
            self.close_segment(&sym)?;
        }
        Ok(())
    }
}

pub fn zstd_compress(source: &Path, output: &Path) -> std::io::Result<String> {
    let temp_output = output.with_extension("zst.tmp");
    let mut temp_file = File::create(&temp_output)?;

    let status = std::process::Command::new("zstd")
        .args(["-q", "-3", "-T1", "-c", "--"])
        .arg(source)
        .stdout(temp_file.try_clone()?)
        .status()?;

    if !status.success() {
        let _ = fs::remove_file(&temp_output);
        return Err(std::io::Error::other("zstd process failed"));
    }

    temp_file.flush()?;
    temp_file.sync_all()?;

    let verify_status = std::process::Command::new("zstd")
        .args(["-q", "-t", "--"])
        .arg(&temp_output)
        .status()?;

    if !verify_status.success() {
        let _ = fs::remove_file(&temp_output);
        return Err(std::io::Error::other("zstd verification failed"));
    }

    // Calculate sha256
    let mut hasher = Sha256::new();
    let mut file = File::open(&temp_output)?;
    let mut buf = [0u8; 65536];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    let digest = hex::encode(hasher.finalize());

    fs::rename(&temp_output, output)?;
    if let Some(parent) = output.parent() {
        sync_directory(parent)?;
    }
    Ok(digest)
}

pub fn sync_directory(path: &Path) -> std::io::Result<()> {
    let file = File::open(path)?;
    file.sync_all()
}

pub fn write_status_json(root: &Path, payload: &serde_json::Value) -> std::io::Result<()> {
    let target = root.join("status.json");
    let temp = root.join(format!(".status.json.{}.tmp", std::process::id()));

    let mut file = File::create(&temp)?;
    let bytes = serde_json::to_vec_pretty(payload)?;
    file.write_all(&bytes)?;
    file.write_all(b"\n")?;
    file.flush()?;
    file.sync_all()?;

    fs::rename(&temp, &target)?;
    sync_directory(root)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_utc_day_hour() {
        let (day, hour) = utc_day_hour(1788382800 * 1_000_000_000);
        assert_eq!(day, "2026-09-02");
        assert_eq!(hour, "21");
    }

    #[tokio::test]
    async fn test_segment_writer_writes_and_compresses() {
        let test_dir = PathBuf::from(format!("/tmp/tape-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&test_dir);

        let mut writer = SegmentWriter::new(&test_dir, 500).unwrap();
        let record = b"{\"kind\":\"public_trade\",\"price\":65000.5,\"qty\":0.1}";
        let now_ns = 1788382800 * 1_000_000_000;

        for i in 0..15 {
            writer.write_record("BTCUSDT", record, now_ns + i).unwrap();
        }

        writer.flush_all().unwrap();

        tokio::time::sleep(std::time::Duration::from_millis(100)).await;

        let seg_dir = test_dir.join("2026-09-02").join("21").join("BTCUSDT");
        assert!(seg_dir.exists(), "symbol segment directory must exist");

        let entries: Vec<_> = fs::read_dir(&seg_dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().to_string())
            .collect();

        assert!(
            entries.iter().any(|name| name.ends_with(".jsonl.zst")),
            "expected .jsonl.zst in {:?}",
            entries
        );

        let manifest_path = test_dir.join("manifest.jsonl");
        assert!(manifest_path.exists());
        let manifest_content = fs::read_to_string(&manifest_path).unwrap();
        assert!(manifest_content.contains("segment_compressed"));
        assert!(manifest_content.contains("BTCUSDT"));

        let _ = fs::remove_dir_all(&test_dir);
    }
}

//! Stamps the build with the git commit it was built from, so the log's Boot
//! record and the heartbeat can name the exact code that traded. Set
//! ENGINE_GIT_COMMIT to override (a build from a tarball); a tree with
//! uncommitted tracked changes gets "-dirty" appended.

use std::process::Command;

fn git(args: &[&str]) -> Option<String> {
    let output = Command::new("git").args(args).output().ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8(output.stdout).ok()?;
    let text = text.trim();
    (!text.is_empty()).then(|| text.to_string())
}

fn main() {
    println!("cargo:rerun-if-env-changed=ENGINE_GIT_COMMIT");
    let commit = match std::env::var("ENGINE_GIT_COMMIT") {
        Ok(value) if !value.trim().is_empty() => value.trim().to_string(),
        _ => {
            let head = git(&["rev-parse", "HEAD"]).unwrap_or_else(|| "unknown".to_string());
            let dirty = git(&["status", "--porcelain", "--untracked-files=no"]).is_some();
            if head != "unknown" && dirty {
                format!("{head}-dirty")
            } else {
                head
            }
        }
    };
    println!("cargo:rustc-env=ENGINE_GIT_COMMIT={commit}");
    if let Some(git_dir) = git(&["rev-parse", "--absolute-git-dir"]) {
        println!("cargo:rerun-if-changed={git_dir}/HEAD");
        println!("cargo:rerun-if-changed={git_dir}/packed-refs");
        if let Some(reference) = git(&["symbolic-ref", "-q", "HEAD"]) {
            println!("cargo:rerun-if-changed={git_dir}/{reference}");
        }
        println!("cargo:rerun-if-changed={git_dir}/index");
    }
}

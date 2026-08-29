//! The venue enum forwards the whole gateway trait, checked against the source.
//!
//! [`Venue`] dispatches by matching one arm per venue, which the compiler
//! checks — but only for the methods that are written down. A trait method
//! with a default body needs no arm to compile, so the enum can silently
//! answer for every adapter with the default while the adapter underneath
//! has a real implementation. Nothing fails; the answer is just wrong
//! everywhere the enum is what the engine holds, which is everywhere.
//!
//! That is not hypothetical. `take_mutation_timing` returns `None` by
//! default, and an enum that inherited the default reported "no timing" for
//! every cancel and amend the Bybit adapter had stamped exactly.
//!
//! The rule enforced here is the blunt one: the enum writes an arm for every
//! method of the trait, defaulted or not. It costs five lines per method and
//! removes the judgment call about which defaults matter.

use std::path::PathBuf;

/// Method names declared directly in a `{...}` block, at one level of
/// indentation. Default bodies nest deeper, so a helper inside one is not
/// mistaken for a declaration.
fn methods_in_block(text: &str, header: &str) -> Vec<String> {
    let start = text
        .find(header)
        .unwrap_or_else(|| panic!("the source no longer contains `{header}`"));
    let body = &text[start + header.len()..];
    let end = body
        .find("\n}")
        .unwrap_or_else(|| panic!("`{header}` has no closing brace at column zero"));
    let mut names = Vec::new();
    for line in body[..end].lines() {
        let Some(rest) = line.strip_prefix("    ") else {
            continue;
        };
        if rest.starts_with(' ') {
            continue;
        }
        let rest = rest.strip_prefix("async ").unwrap_or(rest);
        let Some(rest) = rest.strip_prefix("fn ") else {
            continue;
        };
        let name: String = rest
            .chars()
            .take_while(|c| c.is_alphanumeric() || *c == '_')
            .collect();
        if !name.is_empty() {
            names.push(name);
        }
    }
    names
}

fn engine_types_lib() -> String {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let path = root
        .parent()
        .expect("the crate sits inside the workspace")
        .join("engine-types/src/lib.rs");
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

fn registry_source() -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src/registry.rs");
    std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("{}: {e}", path.display()))
}

#[test]
fn the_venue_enum_forwards_every_gateway_method() {
    let declared = methods_in_block(
        &engine_types_lib(),
        "pub trait VenueGateway: Send + 'static {",
    );
    let forwarded = methods_in_block(&registry_source(), "impl VenueGateway for Venue {");
    assert!(
        declared.len() > 10,
        "the trait scan found almost nothing: {declared:?}"
    );

    let missing: Vec<_> = declared
        .iter()
        .filter(|name| !forwarded.contains(name))
        .collect();
    assert!(
        missing.is_empty(),
        "the venue enum inherits the trait default for {missing:?}, so every adapter's own \
         implementation is unreachable through it. Add a match arm per venue in registry.rs."
    );
}

#[test]
fn the_scan_would_notice_a_method_the_enum_does_not_forward() {
    // Proof the check is not vacuous: the same extractor over a trait and an
    // impl that really do disagree finds the gap.
    let planted_trait = concat!(
        "pub trait Planted: Send + 'static {\n",
        "    fn kept(&self) -> u8;\n",
        "    fn defaulted(&mut self) -> Option<u8> {\n",
        "        None\n",
        "    }\n",
        "}\n"
    );
    let planted_impl = concat!(
        "impl Planted for Wrapper {\n",
        "    fn kept(&self) -> u8 {\n",
        "        match self {\n",
        "            Wrapper::One(inner) => inner.kept(),\n",
        "        }\n",
        "    }\n",
        "}\n"
    );
    let declared = methods_in_block(planted_trait, "pub trait Planted: Send + 'static {");
    let forwarded = methods_in_block(planted_impl, "impl Planted for Wrapper {");
    assert_eq!(declared, vec!["kept", "defaulted"], "{declared:?}");
    assert_eq!(forwarded, vec!["kept"], "{forwarded:?}");
    assert!(
        declared.iter().any(|name| !forwarded.contains(name)),
        "the extractor cannot see a missing forwarder, so the check above proves nothing"
    );
}

#[test]
fn the_timing_hook_the_latency_ledger_reads_is_one_of_them() {
    // The specific method whose absence emptied the cancel and amend timing
    // marks. Named so a future edit that drops the arm fails on the reason
    // rather than on a list diff.
    let forwarded = methods_in_block(&registry_source(), "impl VenueGateway for Venue {");
    assert!(
        forwarded.iter().any(|name| name == "take_mutation_timing"),
        "cancel and amend would log no socket-write or ack stamp: {forwarded:?}"
    );
}

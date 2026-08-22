//! The arming switch, read from a real process environment.
//!
//! `arming.rs` unit-tests the rule with the switch supplied as a bool, which
//! covers the truth table but not the reading of it. This file covers the
//! reading: that `REAL_MONEY` is the variable consulted, that the credential
//! variables consulted are the realm's own, and that a mainnet gateway on an
//! unarmed host stops before it opens a socket.
//!
//! It is deliberately **one test in one file**. Cargo gives each integration
//! test file its own binary, and tests inside a binary share the process
//! environment and run in parallel — so a second test here would race this
//! one. Everything that does not need the real environment belongs in the
//! unit tests, not here.
//!
//! The credentials set below are obvious fakes. Nothing in this file reaches
//! the network: every assertion is about a construction that fails, or about
//! which variable was read.

use engine_venue::VenueRealm;

/// Restore the environment on the way out however the test ends, so a failure
/// partway through cannot leave `REAL_MONEY` set for anything else on the box.
struct Restore(Vec<(&'static str, Option<String>)>);

impl Restore {
    fn capture(names: &[&'static str]) -> Self {
        Restore(
            names
                .iter()
                .map(|n| (*n, std::env::var(n).ok()))
                .collect(),
        )
    }
}

impl Drop for Restore {
    fn drop(&mut self) {
        for (name, previous) in &self.0 {
            match previous {
                Some(value) => std::env::set_var(name, value),
                None => std::env::remove_var(name),
            }
        }
    }
}

#[test]
fn the_arming_switch_and_the_realms_own_variables_are_what_get_read() {
    let (demo_key, demo_secret) = VenueRealm::Demo.credential_vars();
    let (main_key, main_secret) = VenueRealm::Mainnet.credential_vars();
    let _restore = Restore::capture(&[
        "REAL_MONEY",
        demo_key,
        demo_secret,
        main_key,
        main_secret,
    ]);

    // A clean slate: unarmed, with a credential pair present for each realm so
    // that a refusal below is about arming and never about a missing key.
    std::env::remove_var("REAL_MONEY");
    std::env::set_var(demo_key, "demo-key-not-real");
    std::env::set_var(demo_secret, "demo-secret-not-real");
    std::env::set_var(main_key, "main-key-not-real");
    std::env::set_var(main_secret, "main-secret-not-real");

    // Unarmed: demo builds, mainnet does not.
    let demo = VenueRealm::Demo.credentials().expect("demo should build unarmed");
    assert_eq!(demo.realm(), VenueRealm::Demo.as_str());
    assert_eq!(demo.key(), "demo-key-not-real", "demo read the wrong variable");

    let refused = VenueRealm::Mainnet.credentials()
        .expect_err("mainnet built on an unarmed host");
    assert!(
        refused.to_string().contains("REAL_MONEY"),
        "the refusal should name the switch: {refused}"
    );

    // Armed: the pair swaps over. This is the case that matters — it is the
    // only state in which the engine can reach the funded account, and it is
    // reached only by the owner setting this in the host credential file.
    std::env::set_var("REAL_MONEY", "true");
    let main = VenueRealm::Mainnet.credentials().expect("mainnet should build armed");
    assert_eq!(main.realm(), VenueRealm::Mainnet.as_str());
    assert_eq!(
        main.key(),
        "main-key-not-real",
        "mainnet read the wrong variable — a demo key must never authenticate mainnet"
    );

    let demo_refused =
        VenueRealm::Demo.credentials().expect_err("demo built on an armed host");
    assert!(
        demo_refused.to_string().contains("REAL_MONEY"),
        "the refusal should name the switch: {demo_refused}"
    );

    // A typo stops the engine rather than reading as "not armed". Without
    // this, `REAL_MONEY=ture` on an armed host silently runs demo.
    std::env::set_var("REAL_MONEY", "ture");
    for realm in [VenueRealm::Demo, VenueRealm::Mainnet] {
        let err = realm.credentials()
            .unwrap_err_or_panic(&format!("{realm} accepted a typo'd arming switch"));
        assert!(err.contains("ture"), "{err} should quote the bad value");
    }

    // An empty value is a clear no, not a typo: demo runs, mainnet does not.
    std::env::set_var("REAL_MONEY", "");
    assert!(VenueRealm::Demo.credentials().is_ok());
    assert!(VenueRealm::Mainnet.credentials().is_err());
}

/// Small helper so the loop above reads as one line per realm.
trait UnwrapErrOrPanic {
    fn unwrap_err_or_panic(self, message: &str) -> String;
}

impl<T> UnwrapErrOrPanic for Result<T, engine_types::VenueError> {
    fn unwrap_err_or_panic(self, message: &str) -> String {
        match self {
            Ok(_) => panic!("{message}"),
            Err(err) => err.to_string(),
        }
    }
}

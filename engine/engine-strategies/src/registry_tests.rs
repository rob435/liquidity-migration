use engine_types::{Strategy, StrategyId};

use crate::{build_strategy, BuildError};

/// A built strategy is not printable, so `unwrap_err` cannot be used on it.
fn build_err(result: Result<Box<dyn Strategy>, BuildError>) -> BuildError {
    match result {
        Ok(built) => panic!(
            "expected a build error, got the strategy \"{}\"",
            built.name()
        ),
        Err(err) => err,
    }
}

const ID: StrategyId = StrategyId(0);

/// The flat block the research system writes, exactly as the registry
/// documents it. The engine core keeps `name`, `sleeve` and `book_path`, and
/// hands the rest over as the parameter table — mirrored below in the test.
const RESEARCH_BLOCK: &str = r#"
[[strategy]]
name = "touch_sniper"
sleeve = "sniper"
symbol = "BTCUSDT"
side = "buy"
trigger_px = 61000.0
qty = 0.01
stop_px = 60500.0
take_px = 61800.0
ttl_s = 900
"#;

fn params(src: &str) -> toml::Value {
    toml::from_str(src).expect("test config parses")
}

/// Everything required, nothing optional.
fn minimal() -> String {
    "symbol = \"BTCUSDT\"\nside = \"buy\"\ntrigger_px = 100.0\nqty = 2.0\nstop_px = 95.0\n"
        .to_string()
}

/// The minimal config with one line dropped.
fn without(param: &str) -> toml::Value {
    params(&drop_line(param))
}

/// The minimal config with one line replaced. TOML refuses a repeated key, so
/// the old line has to go before the new one arrives.
fn instead_of(param: &str, line: &str) -> toml::Value {
    params(&format!("{}{line}\n", drop_line(param)))
}

fn drop_line(param: &str) -> String {
    minimal()
        .lines()
        .filter(|l| !l.starts_with(&format!("{param} =")))
        .map(|l| format!("{l}\n"))
        .collect()
}

#[test]
fn a_typoed_parameter_is_refused_with_its_name() {
    // "take_price" for "take_px" must not silently mean "no profit level":
    // the research system and the engine ship from one repo, so an unknown
    // key is a mistake, not a version gap.
    let err = build_err(build_strategy(
        "touch_sniper",
        ID,
        &params(&format!("{}take_price = 101.0\n", minimal())),
    ));
    let words = err.to_string();
    assert!(
        words.contains("take_price"),
        "the error names the key: {words}"
    );
    assert!(
        words.contains("take_px"),
        "the error lists the keys that exist: {words}"
    );
}

#[test]
fn the_documented_research_block_builds() {
    let doc: toml::Value = params(RESEARCH_BLOCK);
    let blocks = doc
        .get("strategy")
        .expect("a [[strategy]] array")
        .as_array()
        .expect("an array");
    assert_eq!(blocks.len(), 1);

    let name = blocks[0].get("name").unwrap().as_str().unwrap().to_string();
    // What the engine core does with a flat block: keep its own keys, hand the
    // rest over.
    let mut table = blocks[0].as_table().expect("a table").clone();
    table.remove("name");
    table.remove("sleeve");
    let block_params = toml::Value::Table(table);
    let strategy = build_strategy(&name, StrategyId(3), &block_params).expect("the block builds");

    assert_eq!(strategy.name(), "touch_sniper");
    let subs = strategy.subscriptions();
    assert_eq!(subs.len(), 1);
    assert_eq!(subs[0].symbol, "BTCUSDT");
}

#[test]
fn an_unknown_name_says_so_and_lists_what_it_knows() {
    let err = build_err(build_strategy("moon_lander", ID, &params(&minimal())));
    assert_eq!(
        err,
        BuildError::UnknownStrategy {
            name: "moon_lander".to_string()
        }
    );
    let text = err.to_string();
    assert!(text.contains("moon_lander"), "{text}");
    assert!(text.contains("touch_sniper"), "{text}");
    assert!(text.contains("target_book"), "{text}");
}

/// The follower's block, as the registry documents it.
const FOLLOWER_BLOCK: &str = r#"
[[strategy]]
name = "target_book"
sleeve = "long"
symbols = ["KAITOUSDT", "COTIUSDT"]
"#;

#[test]
fn the_documented_target_book_block_builds_and_subscribes_to_its_universe() {
    let doc: toml::Value = params(FOLLOWER_BLOCK);
    let blocks = doc
        .get("strategy")
        .expect("a [[strategy]] array")
        .as_array()
        .expect("an array");
    let name = blocks[0].get("name").unwrap().as_str().unwrap().to_string();
    let mut table = blocks[0].as_table().expect("a table").clone();
    table.remove("name");
    table.remove("sleeve");
    let strategy =
        build_strategy(&name, StrategyId(1), &toml::Value::Table(table)).expect("the block builds");

    assert_eq!(strategy.name(), "target_book");
    let symbols: Vec<String> = strategy
        .subscriptions()
        .into_iter()
        .map(|s| s.symbol)
        .collect();
    assert_eq!(
        symbols,
        vec!["KAITOUSDT".to_string(), "COTIUSDT".to_string()]
    );
}

#[test]
fn the_followers_unknown_keys_are_refused_by_name_too() {
    let err = build_err(build_strategy(
        "target_book",
        ID,
        &params("symbols = [\"BTCUSDT\"]\nsymbol = \"BTCUSDT\"\n"),
    ));
    let words = err.to_string();
    assert!(
        words.contains("symbol\""),
        "the error names the key: {words}"
    );
    assert!(
        words.contains("symbols"),
        "and the keys that exist: {words}"
    );
}

#[test]
fn a_follower_with_no_universe_is_refused() {
    // The engine collects subscriptions once at boot, so an empty list is a
    // plug that can never trade anything, whatever a later book says.
    for src in ["symbols = []\n", "symbols = [\"\"]\n"] {
        let err = build_err(build_strategy("target_book", ID, &params(src)));
        match &err {
            BuildError::InvalidParam {
                strategy, param, ..
            } => {
                assert_eq!(*strategy, "target_book");
                assert_eq!(*param, "symbols");
            }
            other => panic!("expected an invalid-param error for {src:?}, got {other:?}"),
        }
    }
}

#[test]
fn a_blank_entry_among_real_symbols_is_refused_not_quietly_dropped() {
    // Trading one name fewer than the config asked for is the worst possible
    // answer to a typo, and it would never show up as an error anywhere.
    let err = build_err(build_strategy(
        "target_book",
        ID,
        &params("symbols = [\"BTCUSDT\", \"\"]\n"),
    ));
    assert!(
        matches!(
            err,
            BuildError::InvalidParam {
                param: "symbols",
                ..
            }
        ),
        "{err:?}"
    );
}

#[test]
fn a_symbol_list_that_is_not_a_list_of_strings_is_refused() {
    for src in ["symbols = \"BTCUSDT\"\n", "symbols = [1, 2]\n"] {
        let err = build_err(build_strategy("target_book", ID, &params(src)));
        assert!(
            matches!(
                err,
                BuildError::InvalidParam {
                    param: "symbols",
                    ..
                }
            ),
            "{src:?} gave {err:?}"
        );
    }
}

#[test]
fn the_follower_needs_its_symbols() {
    assert_eq!(
        build_err(build_strategy("target_book", ID, &params("\n"))),
        BuildError::MissingParam {
            strategy: "target_book",
            param: "symbols"
        }
    );
}

#[test]
fn a_missing_required_param_is_named() {
    for param in ["symbol", "side", "trigger_px", "qty", "stop_px"] {
        let err = build_err(build_strategy("touch_sniper", ID, &without(param)));
        assert_eq!(
            err,
            BuildError::MissingParam {
                strategy: "touch_sniper",
                param: leaked(param)
            },
            "dropping {param}"
        );
        assert!(err.to_string().contains(param), "{err} should name {param}");
    }
}

#[test]
fn the_optional_params_may_be_left_out() {
    build_strategy("touch_sniper", ID, &params(&minimal())).expect("the minimum is enough");
}

#[test]
fn a_bad_value_is_named_with_the_reason() {
    struct Case {
        what: &'static str,
        line: &'static str,
        param: &'static str,
    }
    let cases = [
        Case {
            what: "a side that is neither buy nor sell",
            line: "side = \"long\"",
            param: "side",
        },
        Case {
            what: "a side that is not a string",
            line: "side = 1",
            param: "side",
        },
        Case {
            what: "a symbol that is not a string",
            line: "symbol = 42",
            param: "symbol",
        },
        Case {
            what: "an empty symbol",
            line: "symbol = \"\"",
            param: "symbol",
        },
        Case {
            what: "a price written as words",
            line: "trigger_px = \"cheap\"",
            param: "trigger_px",
        },
        Case {
            what: "a size of zero",
            line: "qty = 0.0",
            param: "qty",
        },
        Case {
            what: "a negative size",
            line: "qty = -1.0",
            param: "qty",
        },
        Case {
            what: "a negative stop",
            line: "stop_px = -5.0",
            param: "stop_px",
        },
        Case {
            what: "a stop that is not a number",
            line: "stop_px = true",
            param: "stop_px",
        },
        Case {
            what: "a take level of zero",
            line: "take_px = 0.0",
            param: "take_px",
        },
        Case {
            what: "a take level that is not a number",
            line: "take_px = [1, 2]",
            param: "take_px",
        },
        Case {
            what: "a price that is not a real number",
            line: "trigger_px = nan",
            param: "trigger_px",
        },
        Case {
            what: "a timer of zero seconds",
            line: "ttl_s = 0",
            param: "ttl_s",
        },
        Case {
            what: "a negative timer",
            line: "ttl_s = -30",
            param: "ttl_s",
        },
        Case {
            what: "a timer that is not whole",
            line: "ttl_s = 1.5",
            param: "ttl_s",
        },
    ];

    for case in cases {
        let err = build_err(build_strategy(
            "touch_sniper",
            ID,
            &instead_of(case.param, case.line),
        ));
        match &err {
            BuildError::InvalidParam {
                strategy,
                param,
                detail,
            } => {
                assert_eq!(*strategy, "touch_sniper", "{}", case.what);
                assert_eq!(*param, case.param, "{}", case.what);
                assert!(
                    !detail.is_empty(),
                    "{}: the reason should say something",
                    case.what
                );
            }
            other => panic!(
                "{}: expected an invalid-param error, got {other:?}",
                case.what
            ),
        }
        assert!(err.to_string().contains(case.param), "{}: {err}", case.what);
    }
}

#[test]
fn whole_numbers_are_accepted_where_a_price_or_size_is_wanted() {
    let src = "symbol = \"BTCUSDT\"\nside = \"sell\"\ntrigger_px = 100\nqty = 2\nstop_px = 105\n";
    build_strategy("touch_sniper", ID, &params(src)).expect("integers are numbers too");
}

#[test]
fn params_that_are_not_a_table_are_refused() {
    let value = toml::Value::String("symbol=BTCUSDT".to_string());
    let err = build_err(build_strategy("touch_sniper", ID, &value));
    assert_eq!(
        err,
        BuildError::ParamsNotATable {
            strategy: "touch_sniper",
            got: "a string"
        }
    );
}

/// The error type names params by static string; tests build them from a loop
/// variable, so borrow one from the known set.
fn leaked(param: &str) -> &'static str {
    [
        "symbol",
        "side",
        "trigger_px",
        "qty",
        "stop_px",
        "take_px",
        "ttl_s",
    ]
    .into_iter()
    .find(|known| *known == param)
    .expect("a known parameter name")
}

#[test]
fn every_name_the_registry_advertises_is_one_it_can_reach() {
    // The property the single table buys. When the name list and the builder
    // were two things, a plug could be added to one and not the other: either
    // a name the engine offers and cannot build, or one it builds and will not
    // admit to. Neither can happen from one table, and this says so out loud.
    for name in crate::known_strategies() {
        let err = build_err(build_strategy(name, ID, &params("")));
        assert!(
            !matches!(err, BuildError::UnknownStrategy { .. }),
            "{name} is advertised but the registry does not know it: {err}"
        );
    }
    assert!(
        matches!(
            build_err(build_strategy("no_such_plug", ID, &params(""))),
            BuildError::UnknownStrategy { .. }
        ),
        "a name that is not in the table must still be refused"
    );
}

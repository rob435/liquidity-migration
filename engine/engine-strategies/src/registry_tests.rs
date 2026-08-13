use engine_types::{Strategy, StrategyId};

use crate::{build_strategy, BuildError};

/// A built strategy is not printable, so `unwrap_err` cannot be used on it.
fn build_err(result: Result<Box<dyn Strategy>, BuildError>) -> BuildError {
    match result {
        Ok(built) => panic!("expected a build error, got the strategy \"{}\"", built.name()),
        Err(err) => err,
    }
}

const ID: StrategyId = StrategyId(0);

/// The block the research system writes, exactly as the registry documents it.
const RESEARCH_BLOCK: &str = r#"
[[strategy]]
name = "touch_sniper"

[strategy.params]
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
    "symbol = \"BTCUSDT\"\nside = \"buy\"\ntrigger_px = 100.0\nqty = 2.0\nstop_px = 95.0\n".to_string()
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
fn the_documented_research_block_builds() {
    let doc: toml::Value = params(RESEARCH_BLOCK);
    let blocks = doc.get("strategy").expect("a [[strategy]] array").as_array().expect("an array");
    assert_eq!(blocks.len(), 1);

    let name = blocks[0].get("name").unwrap().as_str().unwrap();
    let block_params = blocks[0].get("params").expect("a [strategy.params] table");
    let strategy = build_strategy(name, StrategyId(3), block_params).expect("the block builds");

    assert_eq!(strategy.name(), "touch_sniper");
    let subs = strategy.subscriptions();
    assert_eq!(subs.len(), 1);
    assert_eq!(subs[0].symbol, "BTCUSDT");
}

#[test]
fn an_unknown_name_says_so_and_lists_what_it_knows() {
    let err = build_err(build_strategy("moon_lander", ID, &params(&minimal())));
    assert_eq!(err, BuildError::UnknownStrategy { name: "moon_lander".to_string() });
    let text = err.to_string();
    assert!(text.contains("moon_lander"), "{text}");
    assert!(text.contains("touch_sniper"), "{text}");
}

#[test]
fn a_missing_required_param_is_named() {
    for param in ["symbol", "side", "trigger_px", "qty", "stop_px"] {
        let err = build_err(build_strategy("touch_sniper", ID, &without(param)));
        assert_eq!(
            err,
            BuildError::MissingParam { strategy: "touch_sniper", param: leaked(param) },
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
        Case { what: "a side that is neither buy nor sell", line: "side = \"long\"", param: "side" },
        Case { what: "a side that is not a string", line: "side = 1", param: "side" },
        Case { what: "a symbol that is not a string", line: "symbol = 42", param: "symbol" },
        Case { what: "an empty symbol", line: "symbol = \"\"", param: "symbol" },
        Case { what: "a price written as words", line: "trigger_px = \"cheap\"", param: "trigger_px" },
        Case { what: "a size of zero", line: "qty = 0.0", param: "qty" },
        Case { what: "a negative size", line: "qty = -1.0", param: "qty" },
        Case { what: "a negative stop", line: "stop_px = -5.0", param: "stop_px" },
        Case { what: "a stop that is not a number", line: "stop_px = true", param: "stop_px" },
        Case { what: "a take level of zero", line: "take_px = 0.0", param: "take_px" },
        Case { what: "a take level that is not a number", line: "take_px = [1, 2]", param: "take_px" },
        Case { what: "a price that is not a real number", line: "trigger_px = nan", param: "trigger_px" },
        Case { what: "a timer of zero seconds", line: "ttl_s = 0", param: "ttl_s" },
        Case { what: "a negative timer", line: "ttl_s = -30", param: "ttl_s" },
        Case { what: "a timer that is not whole", line: "ttl_s = 1.5", param: "ttl_s" },
    ];

    for case in cases {
        let err = build_err(build_strategy(
            "touch_sniper",
            ID,
            &instead_of(case.param, case.line),
        ));
        match &err {
            BuildError::InvalidParam { strategy, param, detail } => {
                assert_eq!(*strategy, "touch_sniper", "{}", case.what);
                assert_eq!(*param, case.param, "{}", case.what);
                assert!(!detail.is_empty(), "{}: the reason should say something", case.what);
            }
            other => panic!("{}: expected an invalid-param error, got {other:?}", case.what),
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
        BuildError::ParamsNotATable { strategy: "touch_sniper", got: "a string" }
    );
}

/// The error type names params by static string; tests build them from a loop
/// variable, so borrow one from the known set.
fn leaked(param: &str) -> &'static str {
    ["symbol", "side", "trigger_px", "qty", "stop_px", "take_px", "ttl_s"]
        .into_iter()
        .find(|known| *known == param)
        .expect("a known parameter name")
}

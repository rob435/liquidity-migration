use engine_types::{Strategy, StrategyId};

use crate::{build_strategy, known_strategies, BuildError};

fn build_err(result: Result<Box<dyn Strategy>, BuildError>) -> BuildError {
    match result {
        Ok(built) => panic!(
            "expected a build error, got the strategy {:?}",
            built.name()
        ),
        Err(error) => error,
    }
}

#[test]
fn registry_contains_only_current_runtime_strategies() {
    assert_eq!(
        known_strategies(),
        [
            "carry_native",
            "long_native",
            "exodus_native",
            "quoter",
            "probe"
        ]
    );
    for retired in ["target_book", "touch_sniper", "template"] {
        assert!(matches!(
            build_err(build_strategy(
                retired,
                StrategyId(0),
                &toml::Value::Table(toml::Table::new()),
            )),
            BuildError::UnknownStrategy { .. }
        ));
    }
}

#[test]
fn every_advertised_name_reaches_a_builder() {
    for name in known_strategies() {
        let error = build_err(build_strategy(
            name,
            StrategyId(0),
            &toml::Value::Table(toml::Table::new()),
        ));
        assert!(
            !matches!(error, BuildError::UnknownStrategy { .. }),
            "{name} is advertised but not registered: {error}"
        );
    }
}

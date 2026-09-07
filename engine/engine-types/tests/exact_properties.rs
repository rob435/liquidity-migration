use engine_types::numeric::{Exact, ExactError};
use proptest::prelude::*;

fn rational() -> impl Strategy<Value = Exact> {
    (any::<i64>(), 1u32..=u32::MAX)
        .prop_map(|(n, d)| Exact::from_ratio(&n.to_string(), &d.to_string()).unwrap())
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(256))]

    #[test]
    fn decimal_exponents_match_integer_scaling(coefficient in any::<i64>(), exponent in -18i32..=18) {
        let scale = 10i128.pow(exponent.unsigned_abs());
        let (n, d) = if exponent >= 0 {
            (i128::from(coefficient) * scale, 1)
        } else {
            (i128::from(coefficient), scale)
        };
        let expected = Exact::from_ratio(&n.to_string(), &d.to_string()).unwrap();
        let actual = Exact::parse_decimal(&format!("{coefficient}e{exponent:+}")).unwrap();
        prop_assert_eq!(&actual, &expected);
        let decimal = actual.to_decimal_string().unwrap();
        prop_assert_eq!(Exact::parse_decimal(&decimal).unwrap(), actual);
    }

    #[test]
    fn ratio_serialization_is_canonical(n in any::<i64>(), d in 1u32..=u32::MAX, factor in 1u32..=u32::MAX) {
        let value = Exact::from_ratio(&n.to_string(), &d.to_string()).unwrap();
        let scaled = Exact::from_ratio(
            &(i128::from(n) * i128::from(factor)).to_string(),
            &(u64::from(d) * u64::from(factor)).to_string(),
        ).unwrap();
        let encoded = serde_json::to_vec(&value).unwrap();
        prop_assert_eq!(&serde_json::to_vec(&scaled).unwrap(), &encoded);
        prop_assert_eq!(serde_json::from_slice::<Exact>(&encoded).unwrap(), value);
    }

    #[test]
    fn arithmetic_preserves_inverse_and_distributivity(a in rational(), b in rational(), c in rational()) {
        prop_assert_eq!(&((&a + &b) - &b), &a);
        prop_assert_eq!(&a * (&b + &c), (&a * &b) + (&a * &c));
        if !b.is_zero() {
            prop_assert_eq!((&a * &b).checked_div(&b).unwrap(), a);
        }
    }

    #[test]
    fn grid_rounding_brackets_value_and_is_idempotent(value in rational(), step_n in 1u32..=u32::MAX, step_d in 1u32..=u32::MAX) {
        let step = Exact::from_ratio(&step_n.to_string(), &step_d.to_string()).unwrap();
        let floor = value.floor_to(&step).unwrap();
        let ceil = value.ceil_to(&step).unwrap();
        prop_assert!(floor <= value && value <= ceil);
        prop_assert!(&value - &floor < step);
        prop_assert!(&ceil - &value < step);
        prop_assert!(floor.is_multiple_of(&step).unwrap());
        prop_assert!(ceil.is_multiple_of(&step).unwrap());
        prop_assert_eq!(floor.floor_to(&step).unwrap(), floor);
        prop_assert_eq!(ceil.ceil_to(&step).unwrap(), ceil);
    }

    #[test]
    fn every_binary64_value_is_preserved_or_explicitly_nonfinite(bits in any::<u64>()) {
        let input = f64::from_bits(bits);
        let result = Exact::from_legacy_f64(input);
        if input.is_finite() {
            let output = result.unwrap().to_f64().unwrap();
            prop_assert_eq!(output.to_bits(), if input == 0.0 { 0 } else { bits });
        } else {
            prop_assert_eq!(result, Err(ExactError::NonFinite));
        }
    }

    #[test]
    fn nonpositive_grid_and_zero_divisor_are_refused(value in rational(), negative in any::<u64>()) {
        let step = Exact::zero() - Exact::from_u64(negative);
        prop_assert_eq!(value.floor_to(&step), Err(ExactError::InvalidStep));
        prop_assert_eq!(value.ceil_to(&step), Err(ExactError::InvalidStep));
        prop_assert_eq!(value.is_multiple_of(&step), Err(ExactError::InvalidStep));
        prop_assert_eq!(value.checked_div(&Exact::zero()), Err(ExactError::DivisionByZero));
    }
}

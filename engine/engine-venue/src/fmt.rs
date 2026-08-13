//! The one place a number becomes a venue string.
//!
//! Every qty, price and stop goes through [`venue_num`]. Bybit reads these
//! as decimal strings and rejects exponent notation, so nothing else in this
//! crate is allowed to format a float.

use engine_types::VenueError;

/// Deepest precision any Bybit tick or step uses, with room to spare.
const MAX_DECIMALS: usize = 10;

/// Render a positive, finite number as a plain decimal string.
///
/// Rust's shortest round-trip form is already plain decimal and exact for a
/// quantized value (`0.001` stays `"0.001"`). It only grows a tail when the
/// caller hands over a value that never went through `quantize`, and that
/// tail is float dust — so it gets rounded off rather than sent.
pub(crate) fn venue_num(value: f64) -> Result<String, VenueError> {
    if !value.is_finite() || value <= 0.0 {
        return Err(VenueError::BadRequest(format!(
            "{value} is not a positive finite number"
        )));
    }
    let short = format!("{value}");
    if decimals(&short) <= MAX_DECIMALS {
        return Ok(short);
    }
    let rounded = trim_trailing_zeros(&format!("{value:.MAX_DECIMALS$}"));
    if rounded.is_empty() || rounded == "0" {
        return Err(VenueError::BadRequest(format!(
            "{value} rounds away to nothing"
        )));
    }
    Ok(rounded)
}

fn decimals(rendered: &str) -> usize {
    match rendered.split_once('.') {
        Some((_, frac)) => frac.len(),
        None => 0,
    }
}

fn trim_trailing_zeros(rendered: &str) -> String {
    if !rendered.contains('.') {
        return rendered.to_string();
    }
    rendered.trim_end_matches('0').trim_end_matches('.').to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(v: f64) -> String {
        venue_num(v).expect("positive finite")
    }

    #[test]
    fn quantized_values_render_exactly() {
        assert_eq!(ok(0.001), "0.001");
        assert_eq!(ok(1.0), "1");
        assert_eq!(ok(123.0), "123");
        assert_eq!(ok(95900.1), "95900.1");
        assert_eq!(ok(0.5), "0.5");
    }

    #[test]
    fn small_prices_never_use_exponent_notation() {
        assert_eq!(ok(0.00000123), "0.00000123");
        assert_eq!(ok(1e-8), "0.00000001");
        for v in [1e-3, 1e-5, 1e-7, 1e-9, 1e-10] {
            let s = ok(v);
            assert!(!s.contains('e') && !s.contains('E'), "{v} rendered as {s}");
        }
    }

    #[test]
    fn large_prices_never_use_exponent_notation() {
        assert_eq!(ok(1e9), "1000000000");
        assert_eq!(ok(123456.789), "123456.789");
        assert!(!ok(1e21).contains('e'));
    }

    #[test]
    fn float_dust_is_shaved_off() {
        assert_eq!(ok(0.1 + 0.2), "0.3");
        assert_eq!(ok(0.000_000_1 * 3.0), "0.0000003");
        assert_eq!(ok(1.1 * 3.0), "3.3");
    }

    #[test]
    fn nothing_unsendable_gets_through() {
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, 0.0, -1.5] {
            assert!(venue_num(bad).is_err(), "{bad} should be refused");
        }
    }
}

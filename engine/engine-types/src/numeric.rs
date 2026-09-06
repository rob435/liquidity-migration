//! Exact financial values; venue decimals and legacy binary64 are distinct inputs.

use std::fmt;
use std::ops::{Add, AddAssign, Mul, Neg, Sub, SubAssign};
use std::str::FromStr;

use num_bigint::BigInt;
use num_rational::BigRational;
use num_traits::{One, Signed, ToPrimitive, Zero};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

/// Input resource limits, not a settlement precision or rounding policy.
pub const MAX_NUMERIC_DIGITS: usize = 4096;
pub const MAX_DECIMAL_EXPONENT: i32 = 4096;
pub const MAX_RATIO_DIGITS: usize = MAX_NUMERIC_DIGITS + MAX_DECIMAL_EXPONENT as usize;

#[derive(Clone, Debug, PartialEq, Eq, thiserror::Error)]
pub enum ExactError {
    #[error("invalid decimal number")]
    InvalidDecimal,
    #[error("numeric input exceeds its bounded decimal or ratio size")]
    InputTooLarge,
    #[error("decimal exponent exceeds {MAX_DECIMAL_EXPONENT}")]
    ExponentOutOfRange,
    #[error("a non-finite legacy number has no exact financial value")]
    NonFinite,
    #[error("division by zero")]
    DivisionByZero,
    #[error("step must be positive")]
    InvalidStep,
    #[error("exact value is outside the requested representation")]
    RepresentationRange,
    #[error("rational denominator must be positive")]
    InvalidDenominator,
    #[error("numeric provenance does not match the exact value")]
    InvalidProvenance,
    #[error("exact execution amounts do not match the compatibility projection")]
    InvalidProjection,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum NumericProvenance {
    VenueDecimal,
    LegacyBinary64 { bits: u64 },
    Derived,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct ExactNumber {
    pub value: Exact,
    pub provenance: NumericProvenance,
}

impl ExactNumber {
    pub fn validate_provenance(&self) -> Result<(), ExactError> {
        let valid = match self.provenance {
            NumericProvenance::VenueDecimal => self.value.to_decimal_string().is_some(),
            NumericProvenance::LegacyBinary64 { bits } => {
                Exact::from_legacy_f64(f64::from_bits(bits)).is_ok_and(|value| value == self.value)
            }
            NumericProvenance::Derived => true,
        };
        if valid {
            Ok(())
        } else {
            Err(ExactError::InvalidProvenance)
        }
    }
    pub fn venue_decimal(text: &str) -> Result<Self, ExactError> {
        Ok(Self {
            value: Exact::parse_decimal(text)?,
            provenance: NumericProvenance::VenueDecimal,
        })
    }
    pub fn legacy_binary64(value: f64) -> Result<Self, ExactError> {
        Ok(Self {
            value: Exact::from_legacy_f64(value)?,
            provenance: NumericProvenance::LegacyBinary64 {
                bits: value.to_bits(),
            },
        })
    }
    pub fn derived(value: Exact) -> Self {
        Self {
            value,
            provenance: NumericProvenance::Derived,
        }
    }
}

impl<'de> Deserialize<'de> for ExactNumber {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Number {
            value: Exact,
            provenance: NumericProvenance,
        }
        let Number { value, provenance } = Number::deserialize(deserializer)?;
        let number = Self { value, provenance };
        number
            .validate_provenance()
            .map_err(serde::de::Error::custom)?;
        Ok(number)
    }
}

/// Unrecognized named assets remain named; absent currency remains unknown.
#[derive(Clone, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum AssetId {
    Named(String),
    #[default]
    Unknown,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct AssetAmount {
    pub asset: AssetId,
    pub amount: ExactNumber,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExecutionAmounts {
    #[serde(default)]
    pub settlement_asset: AssetId,
    pub quantity: ExactNumber,
    pub price: ExactNumber,
    pub fee: Option<AssetAmount>,
}

impl ExecutionAmounts {
    pub fn validate_projection(
        &self,
        qty: f64,
        px: f64,
        fee: Option<f64>,
    ) -> Result<(), ExactError> {
        self.quantity.validate_provenance()?;
        self.price.validate_provenance()?;
        if !self.quantity.value.is_positive()
            || !self.price.value.is_positive()
            || self.quantity.value.to_f64()? != qty
            || self.price.value.to_f64()? != px
        {
            return Err(ExactError::InvalidProjection);
        }
        if let Some(exact_fee) = &self.fee {
            exact_fee.amount.validate_provenance()?;
            if let Some(fee) = fee {
                if exact_fee.amount.value.to_f64()? != fee {
                    return Err(ExactError::InvalidProjection);
                }
            }
        } else if fee.is_some() {
            return Err(ExactError::InvalidProjection);
        }
        Ok(())
    }
}

/// Additional price constraints beyond the finest price tick.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum PricePrecision {
    Tick,
    SignificantFigures {
        max_digits: u32,
        max_decimals: u32,
        integer_exception: bool,
    },
    Unavailable,
}

/// Only venue-observed capabilities are populated; symbol aliases supply no units.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ExactInstrumentSpec {
    pub native_symbol: String,
    pub base_asset: AssetId,
    pub quote_asset: AssetId,
    pub settlement_asset: AssetId,
    pub tick_size: Option<Exact>,
    pub min_price: Option<Exact>,
    pub max_price: Option<Exact>,
    pub price_precision: PricePrecision,
    pub qty_step: Option<Exact>,
    pub min_qty: Option<Exact>,
    pub market_qty_step: Option<Exact>,
    pub market_min_qty: Option<Exact>,
    pub max_qty: Option<Exact>,
    pub max_market_qty: Option<Exact>,
    pub min_notional: Option<Exact>,
    pub contract_multiplier: Option<Exact>,
    pub fee_assets: Option<Vec<AssetId>>,
    pub fee_step: Option<Exact>,
}

/// Reduced rational arithmetic keeps fractional fee allocation exact too.
#[derive(Clone, Debug, Default, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct Exact(BigRational);

impl Exact {
    pub fn zero() -> Self {
        Self(BigRational::zero())
    }
    pub fn one() -> Self {
        Self(BigRational::one())
    }
    pub fn from_i64(value: i64) -> Self {
        Self(BigRational::from_integer(value.into()))
    }
    pub fn from_u64(value: u64) -> Self {
        Self(BigRational::from_integer(value.into()))
    }

    pub fn parse_decimal(text: &str) -> Result<Self, ExactError> {
        if text.len() > MAX_NUMERIC_DIGITS + 16 {
            return Err(ExactError::InputTooLarge);
        }
        let (mantissa, exponent) = if let Some(at) = text.find(['e', 'E']) {
            let exponent = text[at + 1..]
                .parse::<i32>()
                .map_err(|_| ExactError::ExponentOutOfRange)?;
            if exponent.unsigned_abs() > MAX_DECIMAL_EXPONENT as u32 {
                return Err(ExactError::ExponentOutOfRange);
            }
            (&text[..at], exponent)
        } else {
            (text, 0)
        };
        let (negative, mantissa) = match mantissa.as_bytes().first() {
            Some(b'-') => (true, &mantissa[1..]),
            Some(b'+') => (false, &mantissa[1..]),
            _ => (false, mantissa),
        };
        let (whole, fraction) = match mantissa.split_once('.') {
            Some((whole, fraction)) => (whole, fraction),
            None => (mantissa, ""),
        };
        let digits = whole
            .len()
            .checked_add(fraction.len())
            .ok_or(ExactError::InputTooLarge)?;
        if digits > MAX_NUMERIC_DIGITS {
            return Err(ExactError::InputTooLarge);
        }
        if digits == 0
            || !whole
                .bytes()
                .chain(fraction.bytes())
                .all(|byte| byte.is_ascii_digit())
        {
            return Err(ExactError::InvalidDecimal);
        }
        let power = exponent - fraction.len() as i32;
        if power.unsigned_abs() > MAX_DECIMAL_EXPONENT as u32 {
            return Err(ExactError::ExponentOutOfRange);
        }
        let mut coefficient = BigInt::parse_bytes(format!("{whole}{fraction}").as_bytes(), 10)
            .ok_or(ExactError::InvalidDecimal)?;
        if negative {
            coefficient = -coefficient;
        }
        if coefficient.is_zero() {
            return Ok(Self::zero());
        }
        let scale = BigInt::from(10u8).pow(power.unsigned_abs());
        Ok(if power >= 0 {
            Self(BigRational::from_integer(coefficient * scale))
        } else {
            Self(BigRational::new(coefficient, scale))
        })
    }

    /// Preserves the exact binary64 value, not an inferred original decimal.
    pub fn from_legacy_f64(value: f64) -> Result<Self, ExactError> {
        BigRational::from_float(value)
            .map(Self)
            .ok_or(ExactError::NonFinite)
    }

    pub fn from_ratio(numerator: &str, denominator: &str) -> Result<Self, ExactError> {
        let n = parse_integer(numerator)?;
        let d = parse_integer(denominator)?;
        if d <= BigInt::zero() {
            return Err(ExactError::InvalidDenominator);
        }
        Ok(Self(BigRational::new(n, d)))
    }
    pub fn validate_storage(&self) -> Result<(), ExactError> {
        if self.0.numer().bits() > (MAX_RATIO_DIGITS * 4) as u64
            || self.0.denom().bits() > (MAX_RATIO_DIGITS * 4) as u64
            || self.0.numer().to_string().trim_start_matches('-').len() > MAX_RATIO_DIGITS
            || self.0.denom().to_string().len() > MAX_RATIO_DIGITS
        {
            return Err(ExactError::InputTooLarge);
        }
        Ok(())
    }
    pub fn is_zero(&self) -> bool {
        self.0.is_zero()
    }
    pub fn is_positive(&self) -> bool {
        self.0.is_positive()
    }
    pub fn is_negative(&self) -> bool {
        self.0.is_negative()
    }
    pub fn signum(&self) -> i8 {
        if self.is_positive() {
            1
        } else if self.is_negative() {
            -1
        } else {
            0
        }
    }
    pub fn abs(&self) -> Self {
        Self(self.0.abs())
    }
    pub fn checked_div(&self, rhs: &Self) -> Result<Self, ExactError> {
        if rhs.is_zero() {
            return Err(ExactError::DivisionByZero);
        }
        Ok(Self(&self.0 / &rhs.0))
    }
    pub fn floor_to(&self, step: &Self) -> Result<Self, ExactError> {
        if !step.is_positive() {
            return Err(ExactError::InvalidStep);
        }
        Ok(Self((&self.0 / &step.0).floor() * &step.0))
    }
    pub fn ceil_to(&self, step: &Self) -> Result<Self, ExactError> {
        if !step.is_positive() {
            return Err(ExactError::InvalidStep);
        }
        Ok(Self((&self.0 / &step.0).ceil() * &step.0))
    }
    pub fn is_multiple_of(&self, step: &Self) -> Result<bool, ExactError> {
        if !step.is_positive() {
            return Err(ExactError::InvalidStep);
        }
        Ok((&self.0 / &step.0).is_integer())
    }
    pub fn to_u64_exact(&self) -> Result<u64, ExactError> {
        if !self.0.is_integer() {
            return Err(ExactError::RepresentationRange);
        }
        self.0
            .numer()
            .to_u64()
            .ok_or(ExactError::RepresentationRange)
    }
    pub fn to_f64(&self) -> Result<f64, ExactError> {
        self.0
            .to_f64()
            .filter(|value| value.is_finite() && (*value != 0.0 || self.is_zero()))
            .ok_or(ExactError::RepresentationRange)
    }
    /// Finite diagnostic projection for derived values; underflow is zero and overflow saturates.
    /// Native quantities and prices must continue to use `to_f64` validation.
    pub fn reporting_f64(&self) -> f64 {
        self.to_f64().unwrap_or_else(|_| {
            if self.abs() < Self::one() {
                0.0
            } else if self.is_negative() {
                -f64::MAX
            } else {
                f64::MAX
            }
        })
    }
    /// None means a repeating decimal; no silent display rounding occurs.
    pub fn to_decimal_string(&self) -> Option<String> {
        let mut denominator = self.0.denom().clone();
        let mut twos = 0u32;
        let mut fives = 0u32;
        while (&denominator % 2u8).is_zero() {
            denominator /= 2u8;
            twos += 1;
        }
        while (&denominator % 5u8).is_zero() {
            denominator /= 5u8;
            fives += 1;
        }
        if denominator != BigInt::one() {
            return None;
        }
        let scale = twos.max(fives);
        let numerator = self.0.numer()
            * BigInt::from(2u8).pow(scale - twos)
            * BigInt::from(5u8).pow(scale - fives);
        let sign = if numerator.is_negative() { "-" } else { "" };
        let digits = numerator.abs().to_string();
        if scale == 0 {
            return Some(format!("{sign}{digits}"));
        }
        let scale = scale as usize;
        if digits.len() <= scale {
            return Some(format!(
                "{sign}0.{}{digits}",
                "0".repeat(scale - digits.len())
            ));
        }
        let at = digits.len() - scale;
        Some(format!("{sign}{}.{}", &digits[..at], &digits[at..]))
    }
}

fn parse_integer(text: &str) -> Result<BigInt, ExactError> {
    let digits = text.strip_prefix('-').unwrap_or(text);
    if digits.len() > MAX_RATIO_DIGITS {
        return Err(ExactError::InputTooLarge);
    }
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(ExactError::InvalidDecimal);
    }
    BigInt::from_str(text).map_err(|_| ExactError::InvalidDecimal)
}

impl FromStr for Exact {
    type Err = ExactError;
    fn from_str(text: &str) -> Result<Self, Self::Err> {
        Self::parse_decimal(text)
    }
}
impl fmt::Display for Exact {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let Some(decimal) = self.to_decimal_string() {
            f.write_str(&decimal)
        } else {
            write!(f, "{}/{}", self.0.numer(), self.0.denom())
        }
    }
}
impl Serialize for Exact {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        #[derive(Serialize)]
        struct Ratio {
            n: String,
            d: String,
        }
        let ratio = Ratio {
            n: self.0.numer().to_string(),
            d: self.0.denom().to_string(),
        };
        if ratio.n.trim_start_matches('-').len() > MAX_RATIO_DIGITS
            || ratio.d.len() > MAX_RATIO_DIGITS
        {
            return Err(serde::ser::Error::custom(ExactError::InputTooLarge));
        }
        ratio.serialize(serializer)
    }
}
impl<'de> Deserialize<'de> for Exact {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(deny_unknown_fields)]
        struct Ratio {
            n: String,
            d: String,
        }
        let Ratio { n, d } = Ratio::deserialize(deserializer)?;
        Self::from_ratio(&n, &d).map_err(serde::de::Error::custom)
    }
}
macro_rules! arithmetic {
    ($trait:ident, $method:ident, $op:tt) => {
        impl $trait<&Exact> for &Exact {
            type Output = Exact;
            fn $method(self, rhs: &Exact) -> Exact { Exact(&self.0 $op &rhs.0) }
        }
        impl $trait<Exact> for Exact {
            type Output = Exact;
            fn $method(self, rhs: Exact) -> Exact { Exact(self.0 $op rhs.0) }
        }
        impl $trait<&Exact> for Exact {
            type Output = Exact;
            fn $method(self, rhs: &Exact) -> Exact { Exact(self.0 $op &rhs.0) }
        }
        impl $trait<Exact> for &Exact {
            type Output = Exact;
            fn $method(self, rhs: Exact) -> Exact { Exact(&self.0 $op rhs.0) }
        }
    };
}
arithmetic!(Add, add, +);
arithmetic!(Sub, sub, -);
arithmetic!(Mul, mul, *);
impl AddAssign<&Exact> for Exact {
    fn add_assign(&mut self, rhs: &Exact) {
        self.0 += &rhs.0;
    }
}
impl SubAssign<&Exact> for Exact {
    fn sub_assign(&mut self, rhs: &Exact) {
        self.0 -= &rhs.0;
    }
}
impl AddAssign<Exact> for Exact {
    fn add_assign(&mut self, rhs: Exact) {
        self.0 += rhs.0;
    }
}
impl SubAssign<Exact> for Exact {
    fn sub_assign(&mut self, rhs: Exact) {
        self.0 -= rhs.0;
    }
}
impl Neg for Exact {
    type Output = Self;
    fn neg(self) -> Self {
        Self(-self.0)
    }
}
impl Neg for &Exact {
    type Output = Exact;
    fn neg(self) -> Exact {
        Exact(-&self.0)
    }
}
impl std::iter::Sum for Exact {
    fn sum<I: Iterator<Item = Self>>(iter: I) -> Self {
        iter.fold(Self::zero(), |sum, value| sum + value)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn dec(text: &str) -> Exact {
        Exact::parse_decimal(text).unwrap()
    }
    #[test]
    fn lexical_precision_and_grid_arithmetic_never_visit_binary64() {
        let x = dec("9007199254740993.0000000000000000001");
        assert_eq!(
            x.to_decimal_string().unwrap(),
            "9007199254740993.0000000000000000001"
        );
        assert_eq!(dec("0.1") + dec("0.2"), dec("0.3"));
        assert_eq!(dec("112.35").floor_to(&dec("0.05")).unwrap(), dec("112.35"));
        assert_eq!(dec("-1.001").floor_to(&dec("0.01")).unwrap(), dec("-1.01"));
        assert_eq!(dec("-1.001").ceil_to(&dec("0.01")).unwrap(), dec("-1.00"));
        assert_eq!(
            dec("1000000000000.75").floor_to(&Exact::one()).unwrap(),
            dec("1000000000000")
        );
    }
    #[test]
    fn fractional_fee_shares_conserve_reported_amount_exactly() {
        let fee = dec("-0.000000000000000001");
        let third = fee.checked_div(&Exact::from_u64(3)).unwrap();
        assert_eq!(&third + &third + &third, fee);
        assert!(third.to_decimal_string().is_none());
        let encoded = serde_json::to_string(&third).unwrap();
        assert_eq!(serde_json::from_str::<Exact>(&encoded).unwrap(), third);
    }
    #[test]
    fn legacy_input_preserves_bits_without_claiming_venue_decimal_precision() {
        let legacy = Exact::from_legacy_f64(0.1).unwrap();
        assert_ne!(legacy, dec("0.1"));
        assert_eq!(legacy.to_f64().unwrap().to_bits(), 0.1f64.to_bits());
        assert_eq!(
            Exact::from_legacy_f64(f64::from_bits(1))
                .unwrap()
                .to_f64()
                .unwrap()
                .to_bits(),
            1
        );
        assert_eq!(Exact::from_legacy_f64(f64::NAN), Err(ExactError::NonFinite));
    }
    #[test]
    fn invalid_and_amplifying_numbers_are_refused_before_bigint_construction() {
        for text in [
            "",
            "-",
            ".",
            "NaN",
            "inf",
            "1.2.3",
            " 1",
            "1e",
            "1e9999999999999999999",
            "1e-4097",
        ] {
            assert!(Exact::parse_decimal(text).is_err(), "{text}");
        }
        assert_eq!(
            Exact::parse_decimal(&"9".repeat(MAX_NUMERIC_DIGITS + 1)),
            Err(ExactError::InputTooLarge)
        );
        assert!(Exact::from_ratio("1", "0").is_err());
        assert!(Exact::from_ratio("1", "-1").is_err());
        assert!(Exact::from_ratio(&"1".repeat(MAX_RATIO_DIGITS + 1), "1").is_err());
        assert!(dec("1e-4096").to_f64().is_err());
        assert_eq!(
            dec("1").checked_div(&Exact::zero()),
            Err(ExactError::DivisionByZero)
        );
        assert_eq!(
            dec("1").floor_to(&Exact::zero()),
            Err(ExactError::InvalidStep)
        );
    }
    #[test]
    fn serialized_ratios_are_canonical_and_validate_denominators() {
        assert_eq!(
            serde_json::to_string(&Exact::from_ratio("20", "30").unwrap()).unwrap(),
            r#"{"n":"2","d":"3"}"#
        );
        for encoded in [
            r#"{"n":"1","d":"0"}"#,
            r#"{"n":"1","d":"-2"}"#,
            r#"{"n":"1","d":"2","extra":0}"#,
        ] {
            assert!(serde_json::from_str::<Exact>(encoded).is_err());
        }
    }
    #[test]
    fn forged_legacy_provenance_is_rejected_during_durable_decode() {
        let encoded = format!(
            r#"{{"value":{{"n":"1","d":"10"}},"provenance":{{"LegacyBinary64":{{"bits":{}}}}}}}"#,
            0.1f64.to_bits()
        );
        assert!(
            serde_json::from_str::<ExactNumber>(&encoded).is_err(),
            "decimal one-tenth falsely claimed to be exact binary64 0.1"
        );
    }

    #[test]
    fn projections_validate_inventory_and_fee_units_without_erasing_foreign_fees() {
        let mut amounts = ExecutionAmounts {
            settlement_asset: AssetId::Unknown,
            quantity: ExactNumber::venue_decimal("0.3").unwrap(),
            price: ExactNumber::venue_decimal("112.35").unwrap(),
            fee: Some(AssetAmount {
                asset: AssetId::Named("UNRECOGNIZED".into()),
                amount: ExactNumber::venue_decimal("-0.00001").unwrap(),
            }),
        };
        assert!(amounts.validate_projection(0.3, 112.35, None).is_ok());
        assert!(amounts
            .validate_projection(0.3, 112.35, Some(-0.00001))
            .is_ok());
        assert!(amounts.validate_projection(0.3, 112.35, Some(0.0)).is_err());
        assert!(amounts
            .validate_projection(0.30000000000000004, 112.35, None)
            .is_err());
        amounts.quantity = ExactNumber::venue_decimal("1e-400").unwrap();
        assert!(amounts.validate_projection(0.0, 112.35, None).is_err());
        amounts.quantity = ExactNumber::venue_decimal("0").unwrap();
        assert!(amounts.validate_projection(0.0, 112.35, None).is_err());
        amounts.quantity = ExactNumber::venue_decimal("0.3").unwrap();
        amounts.fee = None;
        assert!(amounts.validate_projection(0.3, 112.35, Some(0.0)).is_err());
    }

    #[test]
    fn every_serialized_ratio_is_replayable_with_the_same_resource_bound() {
        let edge =
            Exact::parse_decimal(&format!("{}e4096", "9".repeat(MAX_NUMERIC_DIGITS))).unwrap();
        let encoded = serde_json::to_string(&edge).unwrap();
        assert_eq!(serde_json::from_str::<Exact>(&encoded).unwrap(), edge);
        assert!(serde_json::to_string(&(&edge * &edge)).is_err());
        let tiny = Exact::parse_decimal("1e-4096").unwrap();
        assert_eq!(
            serde_json::from_str::<Exact>(&serde_json::to_string(&tiny).unwrap()).unwrap(),
            tiny
        );
    }
}

//! A small deterministic generator (SplitMix64). One seed reproduces one
//! run; no dependency, no global state.

#[derive(Clone, Debug)]
pub struct Rng {
    state: u64,
}

impl Rng {
    pub fn new(seed: u64) -> Self {
        Rng { state: seed }
    }

    /// An independent stream, so one component's draws never shift
    /// another's.
    pub fn fork(&mut self, salt: u64) -> Self {
        Rng::new(self.next_u64() ^ salt.rotate_left(17))
    }

    pub fn next_u64(&mut self) -> u64 {
        self.state = self.state.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.state;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }

    /// Uniform in `[0, 1)`.
    pub fn unit(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 / (1u64 << 53) as f64
    }

    pub fn chance(&mut self, probability: f64) -> bool {
        probability > 0.0 && self.unit() < probability
    }

    /// Uniform in `0..n`; zero when `n` is zero.
    pub fn below(&mut self, n: u64) -> u64 {
        if n == 0 {
            0
        } else {
            self.next_u64() % n
        }
    }

    pub fn between(&mut self, low: f64, high: f64) -> f64 {
        low + (high - low) * self.unit()
    }
}

#[cfg(test)]
mod tests {
    use super::Rng;

    #[test]
    fn one_seed_is_one_sequence() {
        let a: Vec<u64> = (0..8).map(|_| Rng::new(42).next_u64()).collect();
        let mut rng = Rng::new(42);
        let first = rng.next_u64();
        assert_eq!(a[0], first);
        let mut other = Rng::new(42);
        assert_eq!(
            (0..64).map(|_| rng.next_u64()).collect::<Vec<_>>(),
            (0..65)
                .map(|_| other.next_u64())
                .skip(1)
                .collect::<Vec<_>>()
        );
    }

    #[test]
    fn unit_stays_in_range_and_forks_diverge() {
        let mut rng = Rng::new(7);
        for _ in 0..10_000 {
            let u = rng.unit();
            assert!((0.0..1.0).contains(&u));
        }
        let mut a = rng.fork(1);
        let mut b = rng.fork(2);
        assert_ne!(a.next_u64(), b.next_u64());
    }
}

//! Credential acquisition is private-adapter capability; realms themselves are non-secret metadata.
use crate::{
    BinanceRealm, Credentials, HyperliquidRealm, LighterRealm, MexcRealm, VariationalRealm,
    VenueRealm,
};
use engine_types::VenueError;

pub trait RealmCredentials: Copy {
    fn credentials(self) -> Result<Credentials, VenueError>;
    fn credentials_for_test(self, key: &str, secret: &str) -> Credentials;
}
macro_rules! realm_credentials {
    ($($realm:ty),+ $(,)?) => {$ (
        impl RealmCredentials for $realm {
            fn credentials(self) -> Result<Credentials, VenueError> {
                let (key_var, secret_var) = self.credential_vars();
                Credentials::from_env(self.as_str(), self.is_real_money(), key_var, secret_var)
            }
            fn credentials_for_test(self, key: &str, secret: &str) -> Credentials {
                Credentials::new(self.as_str(), self.is_real_money(), key, secret)
            }
        }
    )+};
}
realm_credentials!(
    VenueRealm,
    BinanceRealm,
    HyperliquidRealm,
    LighterRealm,
    MexcRealm,
    VariationalRealm
);

pub(crate) trait InventoryCredentials {
    fn inventory_credentials(self) -> Result<Credentials, VenueError>;
    fn execution_inventory_credentials(self) -> Result<Credentials, VenueError>;
}
impl InventoryCredentials for VenueRealm {
    fn inventory_credentials(self) -> Result<Credentials, VenueError> {
        let (key_var, secret_var) = self.inventory_credential_vars();
        Credentials::from_env_read_only(self.as_str(), self.is_real_money(), key_var, secret_var)
    }
    fn execution_inventory_credentials(self) -> Result<Credentials, VenueError> {
        let (key_var, secret_var) = self.credential_vars();
        Credentials::from_env_read_only(self.as_str(), self.is_real_money(), key_var, secret_var)
    }
}

use std::ffi::OsStr;
use std::io;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::net::UnixDatagram;

pub(super) fn from_environment() -> Option<UnixDatagram> {
    let address = std::env::var_os("NOTIFY_SOCKET")?;
    if std::env::var("WATCHDOG_PID")
        .ok()
        .is_some_and(|pid| pid.parse::<u32>().ok() != Some(std::process::id()))
    {
        return None;
    }
    match connect(&address) {
        Ok(socket) => Some(socket),
        Err(error) => {
            tracing::error!(%error, "cannot connect the systemd heartbeat socket");
            None
        }
    }
}

fn connect(address: &OsStr) -> io::Result<UnixDatagram> {
    let socket = UnixDatagram::unbound()?;
    socket.set_nonblocking(true)?;
    if address.as_bytes().starts_with(b"@") {
        #[cfg(target_os = "linux")]
        {
            use std::os::linux::net::SocketAddrExt;
            let address =
                std::os::unix::net::SocketAddr::from_abstract_name(&address.as_bytes()[1..])?;
            socket.connect_addr(&address)?;
        }
        #[cfg(not(target_os = "linux"))]
        return Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "abstract notify socket requires Linux",
        ));
    } else {
        socket.connect(address)?;
    }
    Ok(socket)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn notify_socket_carries_loop_liveness_without_blocking() {
        let path = crate::testpath::temp_path("notify-socket");
        let receiver = UnixDatagram::bind(path.path()).unwrap();
        receiver.set_nonblocking(true).unwrap();
        let sender = connect(path.as_os_str()).unwrap();
        sender.send(b"READY=1\nWATCHDOG=1").unwrap();
        let mut bytes = [0; 64];
        let size = receiver.recv(&mut bytes).unwrap();
        assert_eq!(&bytes[..size], b"READY=1\nWATCHDOG=1");
        assert_eq!(
            receiver.recv(&mut bytes).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn notify_supports_systemd_abstract_sockets() {
        use std::os::linux::net::SocketAddrExt;
        let name = format!("engine-notify-{}", std::process::id());
        let address = std::os::unix::net::SocketAddr::from_abstract_name(name.as_bytes()).unwrap();
        let receiver = UnixDatagram::bind_addr(&address).unwrap();
        receiver.set_nonblocking(true).unwrap();
        connect(OsStr::new(&format!("@{name}")))
            .unwrap()
            .send(b"WATCHDOG=1")
            .unwrap();
        let mut bytes = [0; 32];
        assert_eq!(receiver.recv(&mut bytes).unwrap(), b"WATCHDOG=1".len());
    }
}

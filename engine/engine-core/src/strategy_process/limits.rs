use std::process::Command;

pub const MAX_ADDRESS_SPACE_BYTES: u64 = 512 * 1024 * 1024;
pub const MAX_CPU_SECONDS: u64 = 20;

pub(super) fn install(command: &mut Command) {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        // The pre-exec hook uses only stack data and async-signal-safe syscalls.
        unsafe {
            command.pre_exec(apply);
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = command;
}

#[cfg(target_os = "linux")]
fn apply() -> std::io::Result<()> {
    let limits = [
        (libc::RLIMIT_AS, MAX_ADDRESS_SPACE_BYTES),
        (libc::RLIMIT_CPU, MAX_CPU_SECONDS),
        (libc::RLIMIT_NOFILE, 32),
        (libc::RLIMIT_FSIZE, 0),
        (libc::RLIMIT_CORE, 0),
    ];
    for (resource, maximum) in limits {
        let limit = libc::rlimit {
            rlim_cur: maximum as libc::rlim_t,
            rlim_max: maximum as libc::rlim_t,
        };
        if unsafe { libc::setrlimit(resource, &limit) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
    }
    if unsafe { libc::prctl(libc::PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    let statement = |code, k| libc::sock_filter {
        code,
        jt: 0,
        jf: 0,
        k,
    };
    let equal = |k, jt, jf| libc::sock_filter {
        code: 0x15,
        jt,
        jf,
        k,
    };
    let denied = libc::SECCOMP_RET_ERRNO | libc::EPERM as u32;
    let thread_flags = (libc::CLONE_THREAD | libc::CLONE_VM | libc::CLONE_SIGHAND) as u32;
    let mut filter = [
        statement(0x20, 0), // seccomp_data.nr
        equal(libc::SYS_clone3 as u32, 0, 1),
        statement(0x06, libc::SECCOMP_RET_ERRNO | libc::ENOSYS as u32),
        equal(libc::SYS_clone as u32, 0, 4),
        statement(0x20, 16), // seccomp_data.args[0], low flag bits
        statement(0x54, thread_flags),
        equal(thread_flags, 3, 0),
        statement(0x06, denied),
        // fork/vfork are absent on aarch64; clone without CLONE_THREAD is denied above.
        #[cfg(target_arch = "x86_64")]
        equal(libc::SYS_fork as u32, 0, 1),
        #[cfg(target_arch = "x86_64")]
        statement(0x06, denied),
        #[cfg(target_arch = "x86_64")]
        equal(libc::SYS_vfork as u32, 0, 1),
        #[cfg(target_arch = "x86_64")]
        statement(0x06, denied),
        statement(0x06, libc::SECCOMP_RET_ALLOW),
    ];
    // Resolve the thread-success jump independently of architecture-specific syscalls.
    filter[6].jt = (filter.len() - 8) as u8;
    let program = libc::sock_fprog {
        len: filter.len() as u16,
        filter: filter.as_mut_ptr(),
    };
    if unsafe { libc::prctl(libc::PR_SET_SECCOMP, libc::SECCOMP_MODE_FILTER, &program) } != 0 {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

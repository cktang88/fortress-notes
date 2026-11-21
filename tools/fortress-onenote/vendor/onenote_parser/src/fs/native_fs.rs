// Modified by fortress-onenote (vendor/PATCHES.md: P12).
//! Opening files from the native file system (`std::fs`), with a
//! Windows-only path rewrite ([`to_verbatim`]) that routes paths through
//! the `\\?\` verbatim namespace to avoid the DOS-device-name trap.
//!
//! [`open_file`] returns a [`CachedFileSource`] wrapping a
//! [`FileBackedSource`], so large notebooks parse via positional reads
//! against the file handle rather than loading the whole file into
//! memory.

use crate::fs::FileSource;
use crate::fs::file_source::{CachedFileSource, FileBackedSource};
use std::fs::File;
use std::io::Error;
use std::path::PathBuf;
use std::sync::Arc;
use typed_path::TypedPath;

/// Convert a [`TypedPath`] into a host-native [`PathBuf`].
///
/// On Unix the path bytes are reinterpreted as an `OsStr` directly (Unix
/// paths are arbitrary bytes). On Windows the bytes must be valid UTF-8,
/// and the resulting path is additionally rewritten into the verbatim
/// namespace (`\\?\…`) to neutralise the DOS-device-name trap — see
/// [`to_verbatim`].
fn resolve_path(path: TypedPath) -> Result<PathBuf, Error> {
    #[cfg(unix)]
    {
        use std::ffi::OsStr;
        use std::os::unix::ffi::OsStrExt;

        let unix = path
            .with_unix_encoding_checked()
            .map_err(|err| Error::new(std::io::ErrorKind::InvalidInput, err))?;
        Ok(PathBuf::from(OsStr::from_bytes(unix.as_bytes())))
    }
    #[cfg(windows)]
    {
        // The checked conversion rebuilds the path component by component and
        // rejects drive prefixes, so only use it for paths that actually need
        // converting. Windows paths are used as they are.
        let win = if path.is_windows() {
            path.to_path_buf()
        } else {
            path.with_windows_encoding_checked()
                .map_err(|err| Error::new(std::io::ErrorKind::InvalidInput, err))?
        };
        let s = win.to_str().ok_or_else(|| {
            Error::new(
                std::io::ErrorKind::InvalidData,
                "path bytes are not valid UTF-8",
            )
        })?;
        to_verbatim(PathBuf::from(s))
    }
    #[cfg(not(any(unix, windows)))]
    {
        compile_error!("fortress-onenote is only supported on Unix and Windows targets");
    }
}

/// On Windows, rewrite `path` into the verbatim namespace (`\\?\…`).
///
/// Standard Win32 file APIs (which back `std::fs`) interpret a path
/// whose basename matches a reserved DOS device name — `CON`, `PRN`,
/// `AUX`, `NUL`, `COM0`–`COM9`, `LPT0`–`LPT9`, with or without an
/// extension — as a handle to the corresponding device, not as a
/// filename. A `.onetoc2` entry of `COM1` would therefore cause
/// `File::open` to grab the serial port. Routing through `\\?\`
/// disables that parsing and treats the path literally.
///
/// Produces an absolute, backslash-form path with the appropriate
/// `\\?\` (drive) or `\\?\UNC\` (UNC share) prefix.
#[cfg(windows)]
fn to_verbatim(path: PathBuf) -> Result<PathBuf, Error> {
    use std::ffi::OsString;
    let abs = std::path::absolute(&path)?;
    let s = abs.to_string_lossy().replace('/', "\\");
    if s.starts_with(r"\\?\") {
        return Ok(PathBuf::from(s));
    }
    let mut out = OsString::new();
    if s.starts_with(r"\\") {
        // UNC share path → \\?\UNC\server\share\...
        out.push(r"\\?\UNC\");
        out.push(&s[2..]);
    } else {
        out.push(r"\\?\");
        out.push(&s);
    }
    Ok(PathBuf::from(out))
}

/// Opens the file as an on-demand [`FileSource`].
///
/// Each [`FileSource::read_at`] call issues a positional read against
/// the open file handle (`pread` on Unix, overlapped `ReadFile` on
/// Windows), so the file's bytes never need to live in process memory
/// in their entirety — multi-GB notebooks parse with a working set
/// proportional to active reads, not file size. The kernel's page
/// cache fronts repeated reads cheaply.
///
/// # File mutation during parsing
///
/// Reads are positional and recoverable: a truncated or replaced file
/// surfaces as an [`std::io::Error`] (or short read) from `read_at`,
/// not a signal. The parse may still produce garbage or
/// `MalformedData` if a concurrent writer mutates bytes the parser
/// has yet to read — there's no way to make that consistent — but the
/// process won't be aborted.
pub(crate) fn open_file(path: TypedPath) -> Result<Arc<dyn FileSource>, Error> {
    let path = resolve_path(path)?;
    let file = File::open(&path)?;
    let byte_length = file.metadata()?.len();
    let raw = FileBackedSource { file, byte_length };
    Ok(Arc::new(CachedFileSource::new(raw)))
}

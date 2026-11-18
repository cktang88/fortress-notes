//! Input handling: safe zip extraction and notebook discovery.

use anyhow::{Context, Result, bail};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};

/// Maximum number of zip entries we are willing to process.
pub const MAX_ZIP_ENTRIES: usize = 200_000;
/// Maximum total uncompressed bytes extracted from one zip.
pub const MAX_ZIP_TOTAL_BYTES: u64 = 32 * 1024 * 1024 * 1024;
/// Entries larger than this with a compression ratio above
/// [`MAX_COMPRESSION_RATIO`] are treated as zip bombs.
const BOMB_MIN_BYTES: u64 = 64 * 1024 * 1024;
const MAX_COMPRESSION_RATIO: u64 = 200;
const MAX_ERRORS_TXT: u64 = 64 * 1024;

pub const RECYCLE_BIN: &str = "OneNote_RecycleBin";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum InputKind {
    Zip,
    Directory,
    Toc,
    Section,
    Package,
}

impl InputKind {
    pub fn as_str(self) -> &'static str {
        match self {
            InputKind::Zip => "zip",
            InputKind::Directory => "directory",
            InputKind::Toc => "onetoc2",
            InputKind::Section => "one",
            InputKind::Package => "onepkg",
        }
    }
}

pub fn detect_kind(path: &Path) -> Result<InputKind> {
    let meta = fs::metadata(path).with_context(|| format!("cannot read {}", path.display()))?;
    if meta.is_dir() {
        return Ok(InputKind::Directory);
    }
    let ext = ext_lower(path);
    Ok(match ext.as_str() {
        "zip" => InputKind::Zip,
        "onetoc2" => InputKind::Toc,
        "one" => InputKind::Section,
        "onepkg" => InputKind::Package,
        _ => {
            // Sniff zip by magic.
            let mut head = [0u8; 4];
            let n = File::open(path)?.read(&mut head)?;
            if n == 4 && &head == b"PK\x03\x04" {
                InputKind::Zip
            } else if n == 4 && &head == b"MSCF" {
                InputKind::Package
            } else {
                bail!(
                    "unsupported input {} (expected .zip, folder, .onetoc2, .one or .onepkg)",
                    path.display()
                )
            }
        }
    })
}

/// Messages collected while reading the input.
#[derive(Default, Debug)]
pub struct InputReport {
    pub warnings: Vec<String>,
    pub skipped: Vec<String>,
    /// (relative path, text) of every `errors.txt`.
    pub export_errors: Vec<(String, String)>,
}

/// Validate and normalise one zip entry name into safe relative components.
/// Returns `Err(reason)` for names that must be rejected.
pub fn safe_components(name: &str) -> std::result::Result<Vec<String>, &'static str> {
    if name.is_empty() {
        return Err("empty name");
    }
    let normalized = name.replace('\\', "/");
    if normalized.starts_with('/') {
        return Err("absolute path");
    }
    let mut out = Vec::new();
    for comp in normalized.split('/') {
        match comp {
            "" | "." => continue,
            ".." => return Err("parent directory component"),
            c => {
                if c.contains(':') {
                    return Err("drive letter or stream name");
                }
                if c.chars().any(|ch| ch.is_control()) {
                    return Err("control character in name");
                }
                if c.len() > 255 {
                    return Err("component too long");
                }
                // Windows silently strips trailing dots/spaces; refuse names
                // that only consist of them.
                if c.trim_end_matches(['.', ' ']).is_empty() {
                    return Err("invalid component");
                }
                out.push(c.to_string());
            }
        }
    }
    if out.is_empty() {
        return Err("empty name");
    }
    if out.len() > 64 {
        return Err("path too deep");
    }
    Ok(out)
}

fn is_skipped_component(c: &str) -> bool {
    c.eq_ignore_ascii_case(RECYCLE_BIN) || c == "__MACOSX"
}

/// Extract `zip_path` into `dest` (which must not exist or be empty).
pub fn extract_zip(zip_path: &Path, dest: &Path, report: &mut InputReport) -> Result<()> {
    let file = File::open(zip_path).with_context(|| format!("opening {}", zip_path.display()))?;
    let mut archive = zip::ZipArchive::new(file)
        .with_context(|| format!("{} is not a readable zip file", zip_path.display()))?;
    if archive.len() > MAX_ZIP_ENTRIES {
        bail!("zip has too many entries ({})", archive.len());
    }
    fs::create_dir_all(dest)?;
    let mut total: u64 = 0;
    for i in 0..archive.len() {
        let mut entry = match archive.by_index(i) {
            Ok(e) => e,
            Err(e) => {
                report
                    .warnings
                    .push(format!("zip entry #{i} could not be read: {e}"));
                continue;
            }
        };
        let name = match entry.name() {
            Ok(n) => n.into_owned(),
            Err(_) => String::from_utf8_lossy(entry.name_raw()).into_owned(),
        };
        let comps = match safe_components(&name) {
            Ok(c) => c,
            Err(reason) => {
                report
                    .warnings
                    .push(format!("unsafe zip entry rejected ({reason}): {name:?}"));
                continue;
            }
        };
        if entry.is_dir() {
            continue;
        }
        if entry.is_symlink() {
            report
                .warnings
                .push(format!("symbolic link in zip ignored: {name:?}"));
            continue;
        }
        let rel = comps.join("/");
        if comps.iter().any(|c| is_skipped_component(c)) {
            report.skipped.push(rel);
            continue;
        }
        let base = comps.last().map(String::as_str).unwrap_or("");
        if base.eq_ignore_ascii_case("errors.txt") {
            let mut text = String::new();
            let _ = (&mut entry).take(MAX_ERRORS_TXT).read_to_string(&mut text);
            report.export_errors.push((rel, text.trim().to_string()));
            continue;
        }
        if base == ".DS_Store" || base.starts_with("._") {
            continue;
        }
        let size = entry.size();
        let csize = entry.compressed_size().max(1);
        if size >= BOMB_MIN_BYTES && size / csize > MAX_COMPRESSION_RATIO {
            report.warnings.push(format!(
                "zip entry rejected (suspicious compression ratio): {rel}"
            ));
            continue;
        }
        if total.saturating_add(size) > MAX_ZIP_TOTAL_BYTES {
            bail!("zip expands to more than {MAX_ZIP_TOTAL_BYTES} bytes");
        }
        let mut target = dest.to_path_buf();
        for c in &comps[..comps.len() - 1] {
            target.push(c);
        }
        fs::create_dir_all(&target)?;
        target.push(base);
        // Never overwrite: on case-insensitive file systems two entries can
        // collide; keep the first.
        let mut out = match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)
        {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                report
                    .warnings
                    .push(format!("duplicate zip entry skipped: {rel}"));
                continue;
            }
            Err(e) => return Err(e).with_context(|| format!("creating {}", target.display())),
        };
        // Copy at most the declared size + 1 to detect lying headers.
        let written = io::copy(&mut (&mut entry).take(size.saturating_add(1)), &mut out)
            .with_context(|| format!("extracting {rel}"))?;
        out.flush()?;
        if written > size {
            bail!("zip entry {rel} is larger than declared");
        }
        total += written;
    }
    Ok(())
}

/// Result of unpacking a `.onepkg`.
#[derive(Debug, Default)]
pub struct CabExtract {
    pub warnings: Vec<String>,
    /// `(path in package, error)` of section files that could not be
    /// unpacked.
    pub failed_sections: Vec<(String, String)>,
}

/// Maximum number of files we are willing to extract from one `.onepkg`.
const MAX_CAB_ENTRIES: usize = 100_000;

/// Extract a `.onepkg` package (a Microsoft cabinet) into `dest`, with the
/// same name checks and size limits as zip extraction. Entries that cannot
/// be read are skipped with a warning (their sections are then reported as
/// missing), so one damaged entry does not lose the whole package.
pub fn extract_cab(cab_path: &Path, dest: &Path) -> Result<CabExtract> {
    let file = File::open(cab_path).with_context(|| format!("opening {}", cab_path.display()))?;
    // A corrupt cabinet must never abort the whole conversion, even if the
    // decompressor panics (vendor/PATCHES.md, C1, fixes the known cases).
    let cabinet = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
        cab::Cabinet::new(io::BufReader::new(file))
    }))
    .map_err(|_| anyhow::anyhow!("the package is damaged (cabinet reader crashed)"))?
    .map_err(|e| anyhow::anyhow!("the package is not a readable cabinet file: {e}"))?;
    let mut cabinet = cabinet;
    let names: Vec<(String, u64)> = cabinet
        .folder_entries()
        .flat_map(|f| f.file_entries())
        .map(|e| (e.name().to_string(), u64::from(e.uncompressed_size())))
        .collect();
    if names.len() > MAX_CAB_ENTRIES {
        bail!("package has too many entries ({})", names.len());
    }
    fs::create_dir_all(dest)?;
    let mut warnings = Vec::new();
    let mut failed_sections = Vec::new();
    let mut total: u64 = 0;
    for (name, size) in names {
        let comps = match safe_components(&name) {
            Ok(c) => c,
            Err(reason) => {
                warnings.push(format!(
                    "unsafe package entry rejected ({reason}): {name:?}"
                ));
                continue;
            }
        };
        if comps.iter().any(|c| is_skipped_component(c)) {
            continue;
        }
        if total.saturating_add(size) > MAX_ZIP_TOTAL_BYTES {
            bail!("package expands to more than {MAX_ZIP_TOTAL_BYTES} bytes");
        }
        let rel = comps.join("/");
        let mut target = dest.to_path_buf();
        for c in &comps[..comps.len() - 1] {
            target.push(c);
        }
        fs::create_dir_all(&target)?;
        target.push(&comps[comps.len() - 1]);
        let mut out = match OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&target)
        {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                warnings.push(format!("duplicate package entry skipped: {rel}"));
                continue;
            }
            Err(e) => return Err(e).with_context(|| format!("creating {}", target.display())),
        };
        let copied = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut reader = cabinet.read_file(&name)?;
            io::copy(&mut (&mut reader).take(size.saturating_add(1)), &mut out)
        }));
        let error = match copied {
            Ok(Ok(n)) if n == size => {
                total += n;
                continue;
            }
            Ok(Ok(n)) => format!("damaged ({n} bytes instead of {size})"),
            Ok(Err(e)) => format!("could not be extracted: {e}"),
            Err(_) => "could not be extracted (decompressor crashed)".to_string(),
        };
        drop(out);
        let _ = fs::remove_file(&target);
        if ext_lower(Path::new(&rel)) == "one" {
            failed_sections.push((rel, format!("package entry {error}")));
        } else {
            warnings.push(format!("package entry {rel} {error}; skipped"));
        }
    }
    Ok(CabExtract {
        warnings,
        failed_sections,
    })
}

/// A notebook (or notebook-like set of sections) found in the input.
#[derive(Debug, Clone)]
pub enum NotebookSource {
    /// A folder with a `.onetoc2`.
    Toc {
        toc: PathBuf,
        root: PathBuf,
        name: String,
        rel: String,
    },
    /// A `.onepkg` package.
    Package {
        path: PathBuf,
        name: String,
        rel: String,
    },
    /// `.one` files without a table of contents.
    Loose {
        dir: PathBuf,
        sections: Vec<PathBuf>,
        name: String,
        rel: String,
    },
}

fn rel_of(path: &Path, root: &Path) -> String {
    path.strip_prefix(root)
        .unwrap_or(path)
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join("/")
}

/// Lower-case extension; unlike `Path::extension` this also works for
/// dot-files such as `.onetoc2` (which OneDrive downloads contain).
pub fn ext_lower(p: &Path) -> String {
    let name = file_name(p).to_ascii_lowercase();
    match name.rfind('.') {
        Some(i) => name[i + 1..].to_string(),
        None => String::new(),
    }
}

fn file_name(p: &Path) -> String {
    p.file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// Recursively list `.one` files under `dir`, skipping recycle bins,
/// hidden directories and symlinks. Sorted for determinism.
pub fn list_sections(dir: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    walk(dir, &mut |p, is_dir| {
        if !is_dir && ext_lower(p) == "one" {
            out.push(p.to_path_buf());
        }
    });
    out.sort();
    out
}

pub fn walk(dir: &Path, f: &mut dyn FnMut(&Path, bool)) {
    let Ok(rd) = fs::read_dir(dir) else { return };
    let mut entries: Vec<_> = rd.filter_map(|e| e.ok()).collect();
    entries.sort_by_key(|e| e.file_name());
    for e in entries {
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_symlink() {
            continue;
        }
        let p = e.path();
        let name = e.file_name().to_string_lossy().into_owned();
        if ft.is_dir() {
            if is_skipped_component(&name) || name.starts_with('.') {
                continue;
            }
            f(&p, true);
            walk(&p, f);
        } else if ft.is_file() {
            f(&p, false);
        }
    }
}

/// Find notebooks under `root`.
pub fn discover(root: &Path, report: &mut InputReport) -> Vec<NotebookSource> {
    let mut tocs: BTreeMap<PathBuf, Vec<PathBuf>> = BTreeMap::new();
    let mut ones: Vec<PathBuf> = Vec::new();
    let mut pkgs: Vec<PathBuf> = Vec::new();
    walk(root, &mut |p, is_dir| {
        if is_dir {
            return;
        }
        match ext_lower(p).as_str() {
            "onetoc2" => tocs
                .entry(p.parent().unwrap_or(root).to_path_buf())
                .or_default()
                .push(p.to_path_buf()),
            "one" => ones.push(p.to_path_buf()),
            "onepkg" => pkgs.push(p.to_path_buf()),
            _ => {
                let name = file_name(p);
                if name.eq_ignore_ascii_case("errors.txt") {
                    let text = fs::read(p)
                        .map(|b| {
                            String::from_utf8_lossy(&b[..b.len().min(MAX_ERRORS_TXT as usize)])
                                .trim()
                                .to_string()
                        })
                        .unwrap_or_default();
                    report.export_errors.push((rel_of(p, root), text));
                } else if name != ".DS_Store" {
                    report.skipped.push(rel_of(p, root));
                }
            }
        }
    });
    // Notebook roots: TOC directories with no TOC directory above them.
    let toc_dirs: BTreeSet<PathBuf> = tocs.keys().cloned().collect();
    let roots: Vec<PathBuf> = toc_dirs
        .iter()
        .filter(|d| {
            d.ancestors()
                .skip(1)
                .take_while(|a| a.starts_with(root))
                .all(|a| !toc_dirs.contains(a))
        })
        .cloned()
        .collect();
    let mut out = Vec::new();
    for r in &roots {
        let mut candidates = tocs[r].clone();
        candidates.sort();
        let toc = candidates[0].clone();
        out.push(NotebookSource::Toc {
            name: notebook_name(r, root, &toc),
            rel: rel_of(r, root),
            toc,
            root: r.clone(),
        });
    }
    // Loose sections grouped by directory.
    let mut loose: BTreeMap<PathBuf, Vec<PathBuf>> = BTreeMap::new();
    for one in ones {
        if roots.iter().any(|r| one.starts_with(r)) {
            continue;
        }
        loose
            .entry(one.parent().unwrap_or(root).to_path_buf())
            .or_default()
            .push(one);
    }
    for (dir, sections) in loose {
        out.push(NotebookSource::Loose {
            name: dir_display_name(&dir, root),
            rel: rel_of(&dir, root),
            dir,
            sections,
        });
    }
    for p in pkgs {
        out.push(NotebookSource::Package {
            name: stem(&p),
            rel: rel_of(&p, root),
            path: p,
        });
    }
    out
}

fn stem(p: &Path) -> String {
    p.file_stem()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "Notebook".into())
}

fn dir_display_name(dir: &Path, root: &Path) -> String {
    let n = file_name(dir);
    if n.is_empty() || dir == root && n.starts_with('.') {
        "OneNote".into()
    } else {
        n
    }
}

/// Name for a notebook rooted at `dir`: the folder name (OneNote web export
/// and OneDrive both name the folder after the notebook).
fn notebook_name(dir: &Path, root: &Path, toc: &Path) -> String {
    let n = file_name(dir);
    if !n.is_empty() {
        return n;
    }
    let _ = root;
    stem(toc)
}

/// Inputs prepared for conversion.
pub struct Prepared {
    pub notebooks: Vec<NotebookSource>,
    /// Root used for relative paths.
    pub root: PathBuf,
}

pub fn prepare(
    input: &Path,
    kind: InputKind,
    work_dir: &Path,
    report: &mut InputReport,
) -> Result<Prepared> {
    match kind {
        InputKind::Zip => {
            extract_zip(input, work_dir, report)?;
            Ok(Prepared {
                notebooks: discover(work_dir, report),
                root: work_dir.to_path_buf(),
            })
        }
        InputKind::Directory => Ok(Prepared {
            notebooks: discover(input, report),
            root: input.to_path_buf(),
        }),
        InputKind::Toc => {
            let dir = input
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| PathBuf::from("."));
            let root = dir.parent().map(Path::to_path_buf).unwrap_or(dir.clone());
            Ok(Prepared {
                notebooks: vec![NotebookSource::Toc {
                    name: notebook_name(&dir, &root, input),
                    rel: rel_of(&dir, &root),
                    toc: input.to_path_buf(),
                    root: dir,
                }],
                root,
            })
        }
        InputKind::Section => {
            let dir = input
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| PathBuf::from("."));
            Ok(Prepared {
                notebooks: vec![NotebookSource::Loose {
                    name: dir_display_name(&dir, &dir),
                    rel: String::new(),
                    sections: vec![input.to_path_buf()],
                    dir: dir.clone(),
                }],
                root: dir,
            })
        }
        InputKind::Package => {
            let dir = input
                .parent()
                .map(Path::to_path_buf)
                .unwrap_or_else(|| PathBuf::from("."));
            Ok(Prepared {
                notebooks: vec![NotebookSource::Package {
                    name: stem(input),
                    rel: file_name(input),
                    path: input.to_path_buf(),
                }],
                root: dir,
            })
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use zip::write::SimpleFileOptions;

    #[test]
    fn rejects_unsafe_names() {
        for bad in [
            "../evil.one",
            "a/../../evil",
            "/etc/passwd",
            "\\\\server\\share\\x",
            "C:\\Windows\\x.one",
            "C:evil",
            "a/\u{0}b",
            "a/ . /b",
            "",
            "./",
        ] {
            assert!(safe_components(bad).is_err(), "accepted {bad:?}");
        }
        assert_eq!(
            safe_components("Notebook\\Section Group/./S.one").unwrap(),
            vec!["Notebook", "Section Group", "S.one"]
        );
    }

    fn tmpdir(tag: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("fo-input-{tag}-{}", std::process::id()));
        let _ = fs::remove_dir_all(&d);
        fs::create_dir_all(&d).unwrap();
        d
    }

    fn make_zip(path: &Path, entries: &[(&str, &[u8])]) {
        let mut buf = Cursor::new(Vec::new());
        {
            let mut w = zip::ZipWriter::new(&mut buf);
            let opts = SimpleFileOptions::default();
            for (name, data) in entries {
                w.start_file(*name, opts).unwrap();
                w.write_all(data).unwrap();
            }
            w.finish().unwrap();
        }
        fs::write(path, buf.into_inner()).unwrap();
    }

    #[test]
    fn zip_extraction_is_contained() {
        let d = tmpdir("zip");
        let zip_path = d.join("in.zip");
        make_zip(
            &zip_path,
            &[
                ("Notebook/Open Notebook.onetoc2", b"toc"),
                ("Notebook/S1.one", b"one"),
                ("Notebook/OneNote_RecycleBin/OneNote_DeletedPages.one", b"x"),
                ("../escape.one", b"bad"),
                ("Notebook/../../escape2.one", b"bad"),
                ("/abs.one", b"bad"),
                ("errors.txt", b"Notebook too large to export"),
                ("Notebook/s1.one", b"case-dup"),
                ("__MACOSX/._x", b"junk"),
            ],
        );
        let out = d.join("out");
        let mut report = InputReport::default();
        extract_zip(&zip_path, &out, &mut report).unwrap();
        assert!(out.join("Notebook/S1.one").exists());
        assert!(out.join("Notebook/Open Notebook.onetoc2").exists());
        assert!(!out.join("Notebook/OneNote_RecycleBin").exists());
        assert!(!d.join("escape.one").exists());
        assert!(!d.join("escape2.one").exists());
        assert!(!out.join("errors.txt").exists());
        assert_eq!(report.export_errors.len(), 1);
        assert!(report.export_errors[0].1.contains("too large"));
        assert_eq!(
            report
                .warnings
                .iter()
                .filter(|w| w.contains("unsafe"))
                .count(),
            3
        );
        assert!(report.skipped.iter().any(|s| s.contains("RecycleBin")));

        let mut report2 = InputReport::default();
        let nbs = discover(&out, &mut report2);
        assert_eq!(nbs.len(), 1);
        match &nbs[0] {
            NotebookSource::Toc { name, rel, .. } => {
                assert_eq!(name, "Notebook");
                assert_eq!(rel, "Notebook");
            }
            other => panic!("unexpected {other:?}"),
        }
        fs::remove_dir_all(&d).unwrap();
    }

    #[test]
    fn discovery_handles_several_notebooks_and_loose_sections() {
        let d = tmpdir("disc");
        for (p, data) in [
            ("Docs/A/Open Notebook.onetoc2", "t"),
            ("Docs/A/S.one", "s"),
            ("Docs/A/Group/Open Notebook.onetoc2", "t"),
            ("Docs/A/Group/G.one", "s"),
            ("Docs/B/.onetoc2", "t"),
            ("Docs/B/X.one", "s"),
            ("Loose/L1.one", "s"),
            ("Loose/L2.one", "s"),
            ("Docs/readme.docx", "s"),
            ("Pkg.onepkg", "p"),
        ] {
            let path = d.join(p);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, data).unwrap();
        }
        let mut report = InputReport::default();
        let nbs = discover(&d, &mut report);
        let names: Vec<String> = nbs
            .iter()
            .map(|n| match n {
                NotebookSource::Toc { name, .. } => format!("toc:{name}"),
                NotebookSource::Loose { name, sections, .. } => {
                    format!("loose:{name}:{}", sections.len())
                }
                NotebookSource::Package { name, .. } => format!("pkg:{name}"),
            })
            .collect();
        assert_eq!(names, vec!["toc:A", "toc:B", "loose:Loose:2", "pkg:Pkg"]);
        assert_eq!(report.skipped, vec!["Docs/readme.docx"]);
        fs::remove_dir_all(&d).unwrap();
    }
}

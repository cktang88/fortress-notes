//! End-to-end tests of the `fortress-onenote` binary.
//!
//! The sample-based test only runs when `FORTRESS_ONENOTE_SAMPLES` points to
//! a directory of real OneNote exports (`.zip`, `.one`, `.onetoc2`,
//! `.onepkg`, or notebook folders). Sample files are third-party data and are
//! not part of this repository; see README.md.

use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

fn bin() -> &'static str {
    env!("CARGO_BIN_EXE_fortress-onenote")
}

fn tmp(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("fo-cli-{tag}-{}", std::process::id()));
    let _ = fs::remove_dir_all(&d);
    fs::create_dir_all(&d).unwrap();
    d
}

fn zip_with(path: &Path, entries: &[(&str, &[u8])]) {
    let f = fs::File::create(path).unwrap();
    let mut w = zip::ZipWriter::new(f);
    for (name, data) in entries {
        w.start_file(*name, zip::write::SimpleFileOptions::default())
            .unwrap();
        w.write_all(data).unwrap();
    }
    w.finish().unwrap();
}

#[test]
fn usage_errors_exit_2() {
    let out = Command::new(bin()).output().unwrap();
    assert_eq!(out.status.code(), Some(2));
    let out = Command::new(bin()).arg("--help").output().unwrap();
    assert_eq!(out.status.code(), Some(0));
    assert!(String::from_utf8_lossy(&out.stdout).contains("USAGE"));
}

#[test]
fn export_with_only_errors_txt_is_reported() {
    let d = tmp("errors");
    let z = d.join("Notebook.zip");
    zip_with(
        &z,
        &[("errors.txt", b"The notebook is too large to export.")],
    );
    let out = Command::new(bin())
        .arg(&z)
        .arg("--out")
        .arg(d.join("out"))
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(1));
    let err = String::from_utf8_lossy(&out.stderr);
    assert!(err.contains("too large to export"), "{err}");
    // Extraction directory is cleaned up.
    assert!(!d.join("out/.extracted").exists());
    fs::remove_dir_all(&d).unwrap();
}

#[test]
fn hostile_zip_does_not_escape() {
    let d = tmp("hostile");
    let z = d.join("evil.zip");
    zip_with(
        &z,
        &[
            ("../../escaped.one", b"x"),
            ("/abs.one", b"x"),
            ("ok/../../escaped2.one", b"x"),
        ],
    );
    let out = Command::new(bin())
        .arg(&z)
        .arg("--out")
        .arg(d.join("out"))
        .arg("--keep-extracted")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(1));
    assert!(!d.join("escaped.one").exists());
    assert!(!d.join("escaped2.one").exists());
    assert!(!std::env::temp_dir().join("escaped.one").exists());
    fs::remove_dir_all(&d).unwrap();
}

#[test]
fn garbage_section_is_reported_as_failed() {
    let d = tmp("garbage");
    let one = d.join("Broken.one");
    fs::write(&one, vec![0x42u8; 4096]).unwrap();
    let out_dir = d.join("out");
    let out = Command::new(bin())
        .arg(&one)
        .arg("--out")
        .arg(&out_dir)
        .arg("-q")
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(0), "{:?}", out);
    let m: Value =
        serde_json::from_slice(&fs::read(out_dir.join("manifest.json")).unwrap()).unwrap();
    assert_eq!(m["format_version"], 1);
    let failed = &m["notebooks"][0]["report"]["failed_sections"];
    assert_eq!(failed.as_array().unwrap().len(), 1);
    assert!(failed[0]["reason"].as_str().unwrap().len() > 10);
    assert_eq!(m["report"]["sections_failed"], 1);
    fs::remove_dir_all(&d).unwrap();
}

/// Checked-in `.onepkg` (CAB) fixtures, each one MSZIP-compressed folder:
///
/// - `Notebook.onepkg`: `Old.one` = `section_header(0x1b)`, `Group\Broken.one`
///   = 300 bytes of 0x42;
/// - `Empty.onepkg`: `readme.txt` = `nothing here`;
/// - `P.onepkg`: `A.one` = 20,000 bytes `(i * 7 % 251) as u8`, `B.one` =
///   `section_header(0x2a)`.
///
/// They were generated once with the `cab` crate's `CabinetBuilder` (the
/// `cab_with` helper this file had before `vendor/cab/src/builder.rs` was
/// removed; see its git history). Any CAB writer with MSZIP compression
/// gives equivalent files (e.g. `gcab -c -z`, which stores names with `/`).
fn cab_fixture(name: &str, to: &Path) {
    let from = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name);
    fs::copy(from, to).unwrap();
}

/// First 0x48 bytes of a OneNote section header ([MS-ONESTORE] 2.3.1) with
/// the given file-format version (0x2A: OneNote 2010+, 0x1B: OneNote 2007).
fn section_header(ffv: u8) -> Vec<u8> {
    let mut h = vec![
        0xe4, 0x52, 0x5c, 0x7b, 0x8c, 0xd8, 0xa7, 0x4d, 0xae, 0xb1, 0x53, 0x78, 0xd0, 0x29, 0x96,
        0xd3,
    ];
    h.extend([0x11; 16]); // guidFile
    h.extend([0; 16]); // guidLegacyFileVersion
    h.extend([
        0x3f, 0xdd, 0x9a, 0x10, 0x1b, 0x91, 0xf5, 0x49, 0xa5, 0xd0, 0x17, 0x91, 0xed, 0xc8, 0xae,
        0xd8,
    ]);
    for _ in 0..4 {
        h.extend([ffv, 0, 0, 0]);
    }
    h.extend([0; 8]);
    h
}

fn convert(input: &Path, out_dir: &Path) -> (std::process::Output, Option<Value>) {
    let out = Command::new(bin())
        .arg(input)
        .arg("--out")
        .arg(out_dir)
        .arg("-q")
        .output()
        .unwrap();
    let m = fs::read(out_dir.join("manifest.json"))
        .ok()
        .map(|b| serde_json::from_slice(&b).unwrap());
    (out, m)
}

fn failed_reasons(m: &Value) -> Vec<(String, String)> {
    m["notebooks"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|nb| nb["report"]["failed_sections"].as_array().unwrap().clone())
        .map(|f| {
            (
                f["file"].as_str().unwrap_or("").to_string(),
                f["reason"].as_str().unwrap().to_string(),
            )
        })
        .collect()
}

#[test]
fn failed_sections_get_specific_reasons() {
    // Corpus findings: OneNote 2007 sections, truncated sections and
    // non-OneNote files all got "could not be read (unsupported or damaged)".
    let d = tmp("reasons");
    let src = d.join("in");
    fs::create_dir_all(&src).unwrap();
    fs::write(src.join("Old.one"), section_header(0x1b)).unwrap();
    fs::write(src.join("Truncated.one"), section_header(0x2a)).unwrap();
    fs::write(
        src.join("Html.one"),
        b"<html><body>Not a section</body></html>",
    )
    .unwrap();
    fs::write(src.join("Empty.one"), b"").unwrap();
    let (out, m) = convert(&src, &d.join("out"));
    assert_eq!(out.status.code(), Some(0), "{out:?}");
    let reasons = failed_reasons(&m.unwrap());
    let reason = |f: &str| {
        reasons
            .iter()
            .find(|(file, _)| file == f)
            .map(|(_, r)| r.clone())
            .unwrap_or_else(|| panic!("{f} not failed: {reasons:?}"))
    };
    assert!(reason("Old.one").contains("OneNote 2007"));
    assert!(reason("Truncated.one").contains("incomplete"));
    assert!(reason("Html.one").contains("not a OneNote section"));
    assert!(reason("Empty.one").contains("empty"));
    fs::remove_dir_all(&d).unwrap();
}

#[test]
fn onepkg_sections_are_reported_not_dropped() {
    // Corpus findings: a .onepkg whose sections cannot be read converted to
    // an empty notebook with no failure, and one without a .onetoc2 was
    // rejected as a whole.
    let d = tmp("onepkg");
    let pkg = d.join("Notebook.onepkg");
    cab_fixture("Notebook.onepkg", &pkg);
    let (out, m) = convert(&pkg, &d.join("out"));
    assert_eq!(out.status.code(), Some(0), "{out:?}");
    let m = m.unwrap();
    let nb = &m["notebooks"][0];
    assert_eq!(nb["source_kind"], "onepkg");
    assert_eq!(nb["name"], "Notebook");
    assert_eq!(nb["source_path"], "Notebook.onepkg");
    let reasons = failed_reasons(&m);
    assert_eq!(reasons.len(), 2, "{reasons:?}");
    assert!(
        reasons
            .iter()
            .any(|(f, r)| f == "Notebook.onepkg/Old.one" && r.contains("OneNote 2007")),
        "{reasons:?}"
    );
    assert!(
        reasons
            .iter()
            .any(|(f, _)| f == "Notebook.onepkg/Group/Broken.one"),
        "{reasons:?}"
    );
    assert_eq!(m["report"]["sections_failed"], 2);
    // The unpacked package is cleaned up.
    assert!(!d.join("out/.extracted").exists());

    // A package without any section is reported too.
    let empty = d.join("Empty.onepkg");
    cab_fixture("Empty.onepkg", &empty);
    let (out, m) = convert(&empty, &d.join("out2"));
    assert_eq!(out.status.code(), Some(0), "{out:?}");
    let reasons = failed_reasons(&m.unwrap());
    assert_eq!(reasons.len(), 1);
    assert!(reasons[0].1.contains("no readable sections"));
    fs::remove_dir_all(&d).unwrap();
}

#[test]
fn corrupt_onepkg_never_aborts_the_conversion() {
    // Corpus finding: cab 0.6.0 panicked (exit 101, no manifest) on
    // corrupted packages (folder.rs index out of bounds).
    let d = tmp("cabfuzz");
    let pkg = d.join("P.onepkg");
    cab_fixture("P.onepkg", &pkg);
    let good = fs::read(&pkg).unwrap();
    let mut cases: Vec<Vec<u8>> = Vec::new();
    // Every header byte set to 0xFF, and the file truncated at many points.
    for i in 0..good.len().min(120) {
        let mut v = good.clone();
        v[i] = 0xFF;
        cases.push(v);
    }
    for cut in (8..good.len()).step_by(good.len() / 40 + 1) {
        cases.push(good[..cut].to_vec());
    }
    for (i, data) in cases.iter().enumerate() {
        let p = d.join(format!("c{i}.onepkg"));
        fs::write(&p, data).unwrap();
        let (out, m) = convert(&p, &d.join(format!("o{i}")));
        let code = out.status.code();
        assert!(
            code == Some(0) || code == Some(1),
            "case {i}: {code:?} {}",
            String::from_utf8_lossy(&out.stderr)
        );
        if code == Some(0) {
            // Nothing is silently lost: either sections or failures.
            let m = m.unwrap();
            assert!(
                m["report"]["sections"].as_u64().unwrap()
                    + m["report"]["sections_failed"].as_u64().unwrap()
                    > 0,
                "case {i}"
            );
        }
    }
    fs::remove_dir_all(&d).unwrap();
}

// ---- sample-based checks ------------------------------------------------------

/// Per-page object id bookkeeping: ids seen and (text blocks, with an id).
#[derive(Default)]
struct IdCheck {
    seen: HashSet<String>,
    text_blocks: usize,
    with_id: usize,
}

fn is_object_id(s: &str) -> bool {
    let Some((g, n)) = s.split_once('&') else {
        return false;
    };
    g.len() == 38
        && g.starts_with('{')
        && g.ends_with('}')
        && !n.is_empty()
        && n.chars()
            .all(|c| c.is_ascii_hexdigit() && !c.is_ascii_lowercase())
}

fn check_blocks(blocks: &Value, ctx: &str, n: &mut usize, idc: &mut IdCheck) {
    for b in blocks.as_array().unwrap() {
        *n += 1;
        let ty = b["type"].as_str().unwrap();
        for key in ["object_id", "element_id"] {
            if let Some(id) = b[key].as_str() {
                assert!(is_object_id(id), "{ctx}: malformed {key} {id}");
            }
        }
        if let Some(id) = b["object_id"].as_str() {
            assert!(
                idc.seen.insert(id.to_string()),
                "{ctx}: object id {id} used twice"
            );
        }
        if b["text"].as_str().is_some_and(|t| !t.trim().is_empty()) {
            idc.text_blocks += 1;
            idc.with_id += usize::from(b["object_id"].is_string());
        }
        if let Some(text) = b["text"].as_str() {
            let len = text.chars().count();
            for m in b["marks"].as_array().into_iter().flatten() {
                let s = m["start"].as_u64().unwrap() as usize;
                let e = m["end"].as_u64().unwrap() as usize;
                assert!(
                    s < e && e <= len,
                    "{ctx}: mark {m} outside text of length {len}"
                );
            }
            assert!(
                !text.contains('\u{fddf}'),
                "{ctx}: hyperlink marker leaked into text"
            );
        }
        if ty == "table" {
            for row in b["rows"].as_array().unwrap() {
                for cell in row["cells"].as_array().unwrap() {
                    check_blocks(&cell["blocks"], ctx, n, idc);
                }
            }
        }
    }
}

fn collect_rows(rows: &Value, out: &mut Vec<String>) {
    for r in rows.as_array().unwrap() {
        match r["kind"].as_str().unwrap() {
            "item" => out.push(r["item"].as_str().unwrap().to_string()),
            "columns" => {
                let cols = r["columns"].as_array().unwrap();
                assert!(cols.len() >= 2);
                let total: f64 = cols.iter().map(|c| c["width"].as_f64().unwrap()).sum();
                assert!((total - 1.0).abs() < 0.01, "column widths sum to {total}");
                let xs: Vec<f64> = cols.iter().map(|c| c["x"].as_f64().unwrap()).collect();
                assert!(
                    xs.windows(2).all(|w| w[0] <= w[1]),
                    "columns not left to right"
                );
                for c in cols {
                    collect_rows(&c["rows"], out);
                }
            }
            k => panic!("unknown layout row kind {k}"),
        }
    }
}

fn check_entries(
    entries: &Value,
    ids: &mut HashSet<String>,
    pages: &mut usize,
    blocks: &mut usize,
    coverage: &mut (usize, usize),
) {
    for e in entries.as_array().unwrap() {
        let id = e["id"].as_str().unwrap().to_string();
        assert!(ids.insert(id.clone()), "duplicate id {id}");
        match e["kind"].as_str().unwrap() {
            "section_group" => check_entries(&e["children"], ids, pages, blocks, coverage),
            "section" => {
                for p in e["pages"].as_array().unwrap() {
                    *pages += 1;
                    let pid = p["id"].as_str().unwrap().to_string();
                    assert!(ids.insert(pid.clone()), "duplicate page id {pid}");
                    assert!(p["level"].as_i64().unwrap() >= 0);
                    assert!(p["created"].as_str().unwrap().ends_with('Z'));
                    let item_ids: Vec<String> = p["items"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|i| i["id"].as_str().unwrap().to_string())
                        .collect();
                    let mut laid_out = Vec::new();
                    collect_rows(&p["layout"], &mut laid_out);
                    let mut a = item_ids.clone();
                    a.sort();
                    laid_out.sort();
                    assert_eq!(a, laid_out, "layout must reference every item once");
                    let mut idc = IdCheck::default();
                    for it in p["items"].as_array().unwrap() {
                        check_blocks(&it["blocks"], pid.as_str(), blocks, &mut idc);
                    }
                    coverage.0 += idc.text_blocks;
                    coverage.1 += idc.with_id;
                }
            }
            k => panic!("unknown entry kind {k}"),
        }
    }
}

fn sample_inputs(dir: &Path) -> Vec<PathBuf> {
    let mut v: Vec<PathBuf> = fs::read_dir(dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| {
            p.is_dir()
                || matches!(
                    p.extension()
                        .and_then(|e| e.to_str())
                        .map(str::to_ascii_lowercase)
                        .as_deref(),
                    Some("zip" | "one" | "onetoc2" | "onepkg")
                )
        })
        .collect();
    v.sort();
    v
}

#[test]
fn converts_real_samples_when_configured() {
    let Ok(dir) = std::env::var("FORTRESS_ONENOTE_SAMPLES") else {
        eprintln!("FORTRESS_ONENOTE_SAMPLES not set; skipping sample conversion");
        return;
    };
    let inputs = sample_inputs(Path::new(&dir));
    assert!(!inputs.is_empty(), "no samples in {dir}");
    let base = tmp("samples");
    let mut total_pages = 0;
    let mut coverage = (0usize, 0usize);
    for (i, input) in inputs.iter().enumerate() {
        let out_dir = base.join(format!("o{i}"));
        let out = Command::new(bin())
            .arg(input)
            .arg("--out")
            .arg(&out_dir)
            .arg("-q")
            .output()
            .unwrap();
        if out.status.code() == Some(1) {
            eprintln!(
                "{}: no notebooks ({})",
                input.display(),
                String::from_utf8_lossy(&out.stderr).trim()
            );
            continue;
        }
        assert!(out.status.success(), "{}: {:?}", input.display(), out);
        let summary: Value = serde_json::from_slice(&out.stdout).unwrap();
        let m: Value =
            serde_json::from_slice(&fs::read(out_dir.join("manifest.json")).unwrap()).unwrap();
        assert_eq!(m["format"], "fortress-onenote-manifest");
        assert_eq!(m["format_version"], 1);
        assert_eq!(m["units"]["text_offsets"], "code_points");
        let mut ids = HashSet::new();
        let (mut pages, mut blocks) = (0, 0);
        for nb in m["notebooks"].as_array().unwrap() {
            assert!(ids.insert(nb["id"].as_str().unwrap().to_string()));
            check_entries(
                &nb["children"],
                &mut ids,
                &mut pages,
                &mut blocks,
                &mut coverage,
            );
        }
        assert_eq!(summary["pages"].as_u64().unwrap() as usize, pages);
        // Every asset exists and matches its hash.
        for (name, a) in m["assets"].as_object().unwrap() {
            let data = fs::read(out_dir.join(a["path"].as_str().unwrap())).unwrap();
            let sha = hex::encode(Sha256::digest(&data));
            assert!(name.starts_with(&sha), "{name} hash mismatch");
            assert_eq!(a["size"].as_u64().unwrap() as usize, data.len());
        }
        // No assets outside the store, no leftovers.
        assert!(!out_dir.join(".extracted").exists());
        eprintln!("{}: {pages} pages, {blocks} blocks", input.display());
        total_pages += pages;
    }
    assert!(total_pages > 0);
    // Text blocks carry the parser's object ids (SCHEMA.md, Object ids).
    eprintln!("object ids on {} of {} text blocks", coverage.1, coverage.0);
    assert_eq!(
        coverage.1, coverage.0,
        "object ids on only {} of {} text blocks",
        coverage.1, coverage.0
    );
    fs::remove_dir_all(&base).unwrap();
}

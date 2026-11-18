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

// ---- sample-based checks ------------------------------------------------------

fn check_blocks(blocks: &Value, ctx: &str, n: &mut usize) {
    for b in blocks.as_array().unwrap() {
        *n += 1;
        let ty = b["type"].as_str().unwrap();
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
                    check_blocks(&cell["blocks"], ctx, n);
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
) {
    for e in entries.as_array().unwrap() {
        let id = e["id"].as_str().unwrap().to_string();
        assert!(ids.insert(id.clone()), "duplicate id {id}");
        match e["kind"].as_str().unwrap() {
            "section_group" => check_entries(&e["children"], ids, pages, blocks),
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
                    for it in p["items"].as_array().unwrap() {
                        check_blocks(&it["blocks"], pid.as_str(), blocks);
                    }
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
            check_entries(&nb["children"], &mut ids, &mut pages, &mut blocks);
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
    fs::remove_dir_all(&base).unwrap();
}

//! fortress-onenote: convert exported OneNote notebooks into a JSON manifest
//! plus content-addressed asset files for the Fortress Notes importer.
//!
//! Usage: `fortress-onenote [extract] <input> --out <dir> [--keep-extracted] [--compact] [--quiet]`

mod assets;
mod convert;
mod ink;
mod input;
mod layout;
mod links;
mod manifest;
mod math;
mod objectids;
mod richtext;

use anyhow::{Context, Result, bail};
use manifest::*;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const USAGE: &str = "\
fortress-onenote - convert exported OneNote notebooks for Fortress Notes

USAGE:
    fortress-onenote [extract] <INPUT> --out <DIR> [OPTIONS]

INPUT:
    a .zip (OneNote for the web \"Export notebook\" or OneDrive \"Download\"),
    a folder, a .onetoc2 notebook, a .one section or a .onepkg package.

OUTPUT:
    <DIR>/manifest.json and <DIR>/assets/<sha256>.<ext> (see SCHEMA.md).

OPTIONS:
    -o, --out <DIR>      output directory (created if missing)
        --keep-extracted keep the unpacked zip in <DIR>/.extracted
        --compact        write manifest.json without indentation
    -q, --quiet          no progress output on stderr
    -h, --help           show this help
    -V, --version        show version

On success prints one JSON line to stdout:
    {\"manifest\": \"<DIR>/manifest.json\", \"notebooks\": N, \"pages\": N, ...}
Exit status: 0 success (possibly with warnings in the report),
1 conversion failed, 2 usage error.
";

struct Args {
    input: PathBuf,
    out: PathBuf,
    keep_extracted: bool,
    compact: bool,
    quiet: bool,
}

fn parse_args() -> std::result::Result<Option<Args>, String> {
    let mut input = None;
    let mut out = None;
    let mut keep_extracted = false;
    let mut compact = false;
    let mut quiet = false;
    let mut it = std::env::args_os().skip(1).peekable();
    if it.peek().map(|a| a == "extract").unwrap_or(false) {
        it.next();
    }
    while let Some(a) = it.next() {
        let s = a.to_string_lossy();
        match s.as_ref() {
            "-h" | "--help" => return Ok(None),
            "-V" | "--version" => {
                println!("fortress-onenote {}", env!("CARGO_PKG_VERSION"));
                std::process::exit(0);
            }
            "-o" | "--out" => {
                out = Some(PathBuf::from(it.next().ok_or("--out needs a directory")?));
            }
            "--keep-extracted" => keep_extracted = true,
            "--compact" => compact = true,
            "-q" | "--quiet" => quiet = true,
            x if x.starts_with("--out=") => out = Some(PathBuf::from(&x[6..])),
            x if x.starts_with('-') && x.len() > 1 => return Err(format!("unknown option {x}")),
            _ => {
                if input.is_some() {
                    return Err("only one input may be given".into());
                }
                input = Some(PathBuf::from(a));
            }
        }
    }
    let input = input.ok_or("missing <INPUT>")?;
    let out = out.ok_or("missing --out <DIR>")?;
    Ok(Some(Args {
        input,
        out,
        keep_extracted,
        compact,
        quiet,
    }))
}

fn main() -> ExitCode {
    let args = match parse_args() {
        Ok(Some(a)) => a,
        Ok(None) => {
            print!("{USAGE}");
            return ExitCode::SUCCESS;
        }
        Err(e) => {
            eprintln!("error: {e}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    match run(&args) {
        Ok(summary) => {
            println!("{summary}");
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("error: {e:#}");
            ExitCode::from(1)
        }
    }
}

fn base_name(p: &Path) -> String {
    p.file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "input".into())
}

pub fn run_conversion(
    input: &Path,
    out: &Path,
    keep_extracted: bool,
    log: &mut dyn FnMut(&str),
) -> Result<Manifest> {
    let kind = input::detect_kind(input)?;
    fs::create_dir_all(out).with_context(|| format!("creating {}", out.display()))?;
    let mut store = assets::AssetStore::new(&out.join("assets"))?;
    let work = out.join(".extracted");
    if work.exists() {
        fs::remove_dir_all(&work).with_context(|| format!("cleaning {}", work.display()))?;
    }
    let mut in_report = input::InputReport::default();
    let prepared = input::prepare(input, kind, &work, &mut in_report);
    let result = prepared.map(|prepared| {
        let mut notebooks = Vec::new();
        let mut queue: std::collections::VecDeque<input::NotebookSource> =
            prepared.notebooks.into_iter().collect();
        while let Some(src) = queue.pop_front() {
            let mut extra = Vec::new();
            let mut nb =
                convert::convert_notebook(&src, &prepared.root, &mut store, log, &mut extra);
            convert::finalize(&mut nb);
            notebooks.push(nb);
            queue.extend(extra);
        }
        notebooks
    });
    if kind == input::InputKind::Zip && !keep_extracted {
        let _ = fs::remove_dir_all(&work);
    }
    let mut notebooks = result?;
    convert::dedupe_ids(&mut notebooks);
    if notebooks.is_empty() {
        let mut msg = String::from("no OneNote notebooks or sections found in the input");
        if let Some((f, t)) = in_report.export_errors.first() {
            msg.push_str(&format!(
                "; the export contains {f}: {}",
                t.chars().take(300).collect::<String>()
            ));
        }
        bail!(msg);
    }

    let mut report = GlobalReport {
        export_errors: in_report
            .export_errors
            .into_iter()
            .map(|(file, text)| ExportError { file, text })
            .collect(),
        input_warnings: in_report.warnings,
        skipped: in_report.skipped,
        notebooks: notebooks.len(),
        ..Default::default()
    };
    for nb in &notebooks {
        report.sections += nb.report.counts.sections;
        report.sections_failed += nb.report.counts.sections_failed;
        report.pages += nb.report.counts.pages;
    }
    report.assets = store.assets.len();
    report.assets_bytes = store.total_bytes();

    Ok(Manifest {
        format: FORMAT_NAME,
        format_version: FORMAT_VERSION,
        generator: Generator {
            name: "fortress-onenote",
            version: env!("CARGO_PKG_VERSION"),
            parser: "onenote_parser 2.1",
        },
        units: Units {
            text_offsets: "code_points",
            geometry: "half_inch",
            px_per_geometry_unit: 48.0,
        },
        input: InputInfo {
            kind: kind.as_str().into(),
            name: base_name(input),
        },
        notebooks,
        assets: store.assets,
        report,
    })
}

fn run(args: &Args) -> Result<String> {
    let quiet = args.quiet;
    let mut log = |m: &str| {
        if !quiet {
            eprintln!("fortress-onenote: {m}");
        }
    };
    let manifest = run_conversion(&args.input, &args.out, args.keep_extracted, &mut log)?;
    let path = args.out.join("manifest.json");
    let tmp = args.out.join(".manifest.json.tmp");
    let json = if args.compact {
        serde_json::to_vec(&manifest)?
    } else {
        serde_json::to_vec_pretty(&manifest)?
    };
    fs::write(&tmp, json).with_context(|| format!("writing {}", tmp.display()))?;
    fs::rename(&tmp, &path).with_context(|| format!("writing {}", path.display()))?;
    let r = &manifest.report;
    log(&format!(
        "{} notebook(s), {} section(s) ({} failed), {} page(s), {} asset(s)",
        r.notebooks, r.sections, r.sections_failed, r.pages, r.assets
    ));
    Ok(serde_json::json!({
        "manifest": path.to_string_lossy(),
        "format_version": FORMAT_VERSION,
        "notebooks": r.notebooks,
        "sections": r.sections,
        "sections_failed": r.sections_failed,
        "pages": r.pages,
        "assets": r.assets,
    })
    .to_string())
}

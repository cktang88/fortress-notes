//! Regression tests for the findings of the adversarial OneNote corpus run.
//!
//! They only run when `FORTRESS_ONENOTE_CORPUS` points to that corpus (its
//! files are third-party or generated with third-party tools and are not
//! part of this repository). Paths below are relative to that directory.
//! Synthetic versions of the package, panic and failure-reason findings run
//! everywhere (see `tests/cli.rs` and the unit tests).

use serde_json::Value;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

fn corpus() -> Option<PathBuf> {
    let dir = std::env::var_os("FORTRESS_ONENOTE_CORPUS")?;
    Some(PathBuf::from(dir))
}

fn run(root: &Path, rel: &str) -> Value {
    let input = root.join(rel);
    assert!(input.exists(), "corpus file missing: {}", input.display());
    static RUNS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
    let n = RUNS.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
    let out = std::env::temp_dir().join(format!("fo-corpus-{}-{n}", std::process::id()));
    let _ = fs::remove_dir_all(&out);
    let res = Command::new(env!("CARGO_BIN_EXE_fortress-onenote"))
        .arg(&input)
        .arg("--out")
        .arg(&out)
        .arg("-q")
        .output()
        .unwrap();
    assert_eq!(
        res.status.code(),
        Some(0),
        "{rel}: {}",
        String::from_utf8_lossy(&res.stderr)
    );
    let m: Value = serde_json::from_slice(&fs::read(out.join("manifest.json")).unwrap()).unwrap();
    let _ = fs::remove_dir_all(&out);
    m
}

/// Every JSON object in `v`, depth first.
fn objects(v: &Value) -> Vec<&Value> {
    let mut out = Vec::new();
    fn walk<'a>(v: &'a Value, out: &mut Vec<&'a Value>) {
        match v {
            Value::Object(o) => {
                out.push(v);
                o.values().for_each(|x| walk(x, out));
            }
            Value::Array(a) => a.iter().for_each(|x| walk(x, out)),
            _ => {}
        }
    }
    walk(v, &mut out);
    out
}

fn blocks_of<'a>(m: &'a Value, ty: &str) -> Vec<&'a Value> {
    objects(&m["notebooks"])
        .into_iter()
        .filter(|o| o["type"] == ty)
        .collect()
}

fn failed(m: &Value) -> Vec<&Value> {
    m["notebooks"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|nb| nb["report"]["failed_sections"].as_array().unwrap())
        .collect()
}

fn report(m: &Value) -> (u64, u64, u64) {
    let r = &m["report"];
    (
        r["sections"].as_u64().unwrap(),
        r["sections_failed"].as_u64().unwrap(),
        r["pages"].as_u64().unwrap(),
    )
}

fn tags_of<'a>(m: &'a Value, text: &str) -> Vec<(&'a str, &'a str)> {
    objects(&m["notebooks"])
        .into_iter()
        .find(|o| o["text"].as_str() == Some(text) && o["tags"].is_array())
        .unwrap_or_else(|| panic!("no block {text:?}"))["tags"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| (t["label"].as_str().unwrap(), t["shape"].as_str().unwrap()))
        .collect()
}

#[test]
fn password_protected_sections_are_named_as_such() {
    let Some(c) = corpus() else { return };
    for rel in [
        "aspose-note_Aspose.Note-for-.NET/repo/Examples/Data/NoteBook/Locked Pass1.one",
        "aspose-note_Aspose.Note-for-.NET/repo/Examples/Data/NoteBook/Locked Pass2.one",
        "generated-aspose/gen-password-fortress.one",
    ] {
        let m = run(&c, rel);
        let f = failed(&m);
        assert_eq!(f.len(), 1, "{rel}");
        assert_eq!(
            f[0]["reason"], "The section is password-protected.",
            "{rel}"
        );
    }
}

#[test]
fn one_bad_object_does_not_fail_the_section() {
    let Some(c) = corpus() else { return };
    // Empty table cell, lone surrogate, missing outline author/level.
    for (rel, pages) in [
        ("generated-aspose/gen-table-empty-cell.one", 2),
        ("generated-aspose/gen-lone-surrogate.one", 2),
        ("generated-notecraft-noauthor/nc-pages-links.one", 21),
        ("obsidian-importer/fixtures/makecab-lzx-notebook.onepkg", 1),
    ] {
        let m = run(&c, rel);
        assert_eq!(report(&m), (1, 0, pages), "{rel}");
    }
    let m = run(&c, "generated-aspose/gen-lone-surrogate.one");
    assert!(serde_json::to_string(&m).unwrap().contains('\u{FFFD}'));
    let m = run(&c, "obsidian-importer/fixtures/makecab-lzx-notebook.onepkg");
    assert!(
        serde_json::to_string(&m)
            .unwrap()
            .contains("LZX package content")
    );
    // Sections onenote_parser rejected although they are readable.
    for (rel, pages) in [
        (
            "groupdocs/groupdocs-conversion_GroupDocs.Conversion-for-.NET/sample.one",
            4,
        ),
        (
            "aspose-note_Aspose.Note-for-.NET/repo/Examples/Data/NoteBook/Neuer Abschnitt 1.one",
            1,
        ),
        (
            "onenotegem-extracted/sharednotebookforgroupproject/Meeting notes.one",
            1,
        ),
        (
            "apache-tika/repo/tika-parsers/tika-parsers-standard/tika-parsers-standard-modules/tika-parser-microsoft-module/src/test/resources/test-documents/testOneNote-fuzz3.one",
            2,
        ),
    ] {
        let m = run(&c, rel);
        assert_eq!(report(&m), (1, 0, pages), "{rel}");
    }
}

#[test]
fn every_tag_of_a_paragraph_keeps_its_definition() {
    let Some(c) = corpus() else { return };
    let m = run(
        &c,
        "aspose-note-foss/repo/testfiles/NumberedListWithTags.one",
    );
    let labels: Vec<&str> = tags_of(&m, "First-third").iter().map(|t| t.0).collect();
    assert_eq!(
        labels,
        [
            "Запланировать собрание",
            "Послушать музыку",
            "Контакт",
            "Вопрос",
            "Дела",
            "Важно"
        ]
    );
    let m = run(&c, "generated-aspose/gen-tags.one");
    let shapes: Vec<&str> = tags_of(&m, "five tags on one paragraph")
        .iter()
        .map(|t| t.1)
        .collect();
    assert_eq!(
        shapes,
        [
            "green_check_box",
            "yellow_star",
            "blue_follow_up_flag",
            "blue_circle",
            "light_bulb"
        ]
    );
    // Only check boxes are ever completed.
    for t in objects(&m["notebooks"])
        .into_iter()
        .filter(|o| o["shape"].is_string() && o["checkable"].is_boolean())
    {
        if t["checkable"] == false {
            assert_eq!(t["completed"], false, "{t}");
            assert!(t["completed_at"].is_null(), "{t}");
        }
    }
    // Empty labels are counted under the shape.
    let by_label = &m["notebooks"][0]["report"]["tags_by_label"];
    assert!(by_label.get("").is_none());
    assert!(by_label.get("light_bulb").is_some());
}

#[test]
fn number_formats_with_text_before_the_number() {
    let Some(c) = corpus() else { return };
    let m = run(&c, "generated-aspose/gen-lists.one");
    let markers: Vec<String> = objects(&m["notebooks"])
        .into_iter()
        .filter_map(|o| o["list"].as_object())
        .map(|l| {
            let marker = l["marker"].as_str().unwrap();
            assert!(!marker.contains('\u{FFFD}'), "{l:?}");
            if l["style"] == "bullet" {
                assert!(!marker.contains('%'), "{l:?}");
            }
            marker.to_string()
        })
        .collect();
    for want in ["(%)", "Step %:", "[%]", "§%", "-%-", "%.%"] {
        assert!(
            markers.iter().any(|m| m == want),
            "{want} not in {markers:?}"
        );
    }
}

#[test]
fn data_status_is_consistent_between_block_and_report() {
    let Some(c) = corpus() else { return };
    for rel in [
        "generated-aspose/gen-attachments.one",
        "generated-notecraft/nc-layout-media.one",
    ] {
        let m = run(&c, rel);
        let mut statuses: Vec<String> = Vec::new();
        for b in blocks_of(&m, "file")
            .into_iter()
            .chain(blocks_of(&m, "image"))
        {
            let st = b["data_status"].as_str().unwrap();
            assert_eq!(
                b["asset"].is_null(),
                st != "available" && st != "invalid",
                "{b}"
            );
            if st != "available" {
                statuses.push(st.to_string());
            }
        }
        let mut reported: Vec<String> = objects(&m["notebooks"])
            .into_iter()
            .filter(|o| o["kind"] == "attachment" || o["kind"] == "image")
            .filter_map(|o| o["data_status"].as_str().map(str::to_string))
            .collect();
        statuses.sort();
        reported.sort();
        assert_eq!(statuses, reported, "{rel}");
        assert!(statuses.iter().any(|s| s == "empty"), "{rel}");
    }
    let m = run(&c, "generated-notecraft/nc-layout-media.one");
    assert!(
        blocks_of(&m, "image").iter().any(
            |b| b["data_status"] == "invalid" && b["asset"].as_str().unwrap().ends_with(".bin")
        )
    );
}

#[test]
fn control_characters_never_reach_the_text() {
    let Some(c) = corpus() else { return };
    let m = run(&c, "generated-aspose/gen-unicode.one");
    for o in objects(&m["notebooks"]) {
        for key in ["text", "title"] {
            if let Some(t) = o[key].as_str() {
                assert!(
                    !t.chars()
                        .any(|ch| (ch.is_control() && ch != '\t' && ch != '\n')
                            || (ch as u32) & 0xFFFE == 0xFFFE),
                    "{t:?}"
                );
            }
        }
    }
    // Links are kept exactly as stored (the importer filters schemes).
    let m = run(&c, "generated-aspose/gen-links-subpages.one");
    let s = serde_json::to_string(&m).unwrap();
    assert!(s.contains("javascript:"));
}

#[test]
fn onenote_2007_and_damaged_sections_get_specific_reasons() {
    let Some(c) = corpus() else { return };
    let m = run(
        &c,
        "aspose-note_Aspose.Note-for-.NET/repo/Examples/Data/Loading-and-Saving/OneNote2007.one",
    );
    assert!(
        failed(&m)[0]["reason"]
            .as_str()
            .unwrap()
            .contains("OneNote 2007")
    );
    let tika = "apache-tika/repo/tika-parsers/tika-parsers-standard/tika-parsers-standard-modules/tika-parser-microsoft-module/src/test/resources/test-documents";
    let m = run(&c, &format!("{tika}/testOneNote-fuzz2.one"));
    assert!(
        failed(&m)[0]["reason"]
            .as_str()
            .unwrap()
            .contains("incomplete")
    );
    let m = run(&c, &format!("{tika}/testOneNote-fuzz1.one"));
    assert!(
        failed(&m)[0]["reason"]
            .as_str()
            .unwrap()
            .contains("inconsistent")
    );
}

#[test]
fn onepkg_contents_are_never_silently_lost() {
    let Some(c) = corpus() else { return };
    // OneNote 2007 sections in a package: listed as failed (was: empty
    // notebook, no failure).
    for (rel, n) in [
        ("onenotegem/personal_notebook.onepkg", 7),
        ("onenotegem/onenote_2007_guide.onepkg", 2),
    ] {
        let m = run(&c, rel);
        let f = failed(&m);
        assert_eq!(f.len(), n, "{rel}");
        assert!(
            f.iter()
                .all(|f| f["reason"].as_str().unwrap().contains("2007"))
        );
    }
    // Package without a .onetoc2: its sections (was: rejected as a whole).
    let m = run(&c, "onenotegem/studentnotebook.onepkg");
    assert_eq!(report(&m), (8, 0, 39));
    // Third-party package (was: empty notebook).
    let m = run(&c, "generated-notecraft/nc-adversarial.onepkg");
    let (sections, failed_n, pages) = report(&m);
    assert_eq!((sections, failed_n), (76, 0));
    assert!(pages >= 578);
}

#[test]
fn third_party_tables_of_contents_give_the_order() {
    let Some(c) = corpus() else { return };
    for rel in [
        "generated-notecraft/NoteCraft adversarial",
        "generated-notecraft/nc-adversarial.onepkg",
    ] {
        let m = run(&c, rel);
        let nb = &m["notebooks"][0];
        let names: Vec<&str> = nb["children"]
            .as_array()
            .unwrap()
            .iter()
            .map(|e| e["name"].as_str().unwrap())
            .collect();
        assert_eq!(
            &names[..4],
            [
                "Rich text",
                "Layout & media",
                "Pages: odd/titles \\ : * ? \" < > |",
                "500 pages"
            ],
            "{rel}"
        );
        assert!(names.contains(&"Empty group"), "{rel}: {names:?}");
        for s in objects(&nb["children"])
            .into_iter()
            .filter(|o| o["kind"] == "section")
        {
            assert_eq!(s["in_toc"], true, "{rel}: {}", s["name"]);
        }
    }
}

#[test]
fn equations_from_other_writers_render() {
    let Some(c) = corpus() else { return };
    let m = run(&c, "notecraft-joshmaster2165/onenote-equations.one");
    let math = blocks_of(&m, "math");
    assert_eq!(math.len(), 13);
    for b in math {
        assert!(b["latex"].is_string(), "{b}");
        assert!(b["mathml"].is_string(), "{b}");
    }
}

#[test]
fn fuzzed_crash_repros_convert() {
    let Some(c) = corpus() else { return };
    let dir = c.join("fuzz-mutations/kept-crashes");
    let mut n = 0;
    for e in fs::read_dir(&dir).unwrap() {
        let p = e.unwrap().path();
        let rel = p.strip_prefix(&c).unwrap().to_string_lossy().into_owned();
        let m = run(&c, &rel);
        let (sections, failed_n, _) = report(&m);
        assert!(sections + failed_n > 0, "{rel}");
        n += 1;
    }
    assert!(n >= 30);
}

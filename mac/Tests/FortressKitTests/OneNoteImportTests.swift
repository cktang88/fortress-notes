import Foundation
import Testing

#if canImport(FortressKit)
@testable import FortressKit
#else
@testable import Core
#endif

// The OneNote import job as the backend reports it (contract in the task brief; shapes
// below are exactly what `GET /api/import/onenote/{job_id}` sends).

enum OneNoteFixtures {
    static let queued = #"{"job_id": "job-7f3a", "status": "queued"}"#

    static let importing = #"""
    {"job_id": "job-7f3a", "status": "importing",
     "progress": {"pages_total": 148, "pages_done": 61, "current": "Work › Meetings › Weekly sync 2024-03-11"},
     "result": null, "error": null}
    """#

    static let done = #"""
    {"job_id": "job-7f3a", "status": "done",
     "progress": {"pages_total": 148, "pages_done": 148, "current": null},
     "result": {
       "notebooks": [
         {"name": "Work", "folder_id": "01M7FOLDERWORK000000000000", "sections": 6,
          "pages_created": 92, "pages_updated": 3, "pages_unchanged": 0, "pages_skipped": 1,
          "images": 31, "files": 7, "tables": 12, "columns_pages": 9,
          "warnings": ["Meetings › Kickoff: 1 ink drawing(s) not imported (Fortress does not support ink)",
                       {"page": "Projects › Roadmap", "message": "a video link was kept as a link"}],
          "failed_sections": [{"path": "Work › HR (locked)", "reason": "The section is password-protected.",
                               "hint": "Unlock it in OneNote, export the notebook again and re-import."}]},
         {"name": "Recipes", "folder_id": "01M7FOLDERRECIPES000000000", "sections": 3,
          "pages_created": 52, "pages_updated": 0, "pages_unchanged": 0, "pages_skipped": 0,
          "images": 5, "files": 2, "tables": 0, "columns_pages": 4, "warnings": [], "failed_sections": []}
       ],
       "links": {"rewritten": 214, "unresolved": [
          {"page": "Work › Projects › Roadmap", "target": "onenote:#Q3%20Goals&section-id={6B1F}"},
          {"page": "Recipes › Bread › Sourdough", "target": "onenote:https://d.docs.live.net/…/Baking.one#Starter"}]},
       "errors_txt": "Work/HR.one: encrypted section skipped\n",
       "duration_ms": 41870},
     "error": null}
    """#

    static let failed = #"""
    {"job_id": "job-7f3a", "status": "failed", "progress": {"pages_total": 0, "pages_done": 0, "current": null},
     "result": null, "error": "OneNote converter not found: set FORTRESS_ONENOTE_BIN or build fortress-onenote"}
    """#

    static let recent = #"""
    {"jobs": [{"job_id": "job-7f3a", "status": "done", "created_at": "2026-11-18T09:12:00+00:00", "filename": "Work.zip"},
              {"job_id": "job-6e21", "status": "failed", "error": "The file is not a OneNote export"}]}
    """#

    static func job(_ json: String) throws -> OneNoteImportJob {
        try JSONDecoder().decode(OneNoteImportJob.self, from: Data(json.utf8))
    }
}

@Suite struct OneNoteImportDecodingTests {
    @Test func decodesQueuedJob() throws {
        let job = try OneNoteFixtures.job(OneNoteFixtures.queued)
        #expect(job.jobId == "job-7f3a")
        #expect(job.status == .queued)
        #expect(job.progress == .init())
        #expect(job.result == nil)
        #expect(OneNoteImport.step(for: job.status) == .convert)
    }

    @Test func decodesProgress() throws {
        let job = try OneNoteFixtures.job(OneNoteFixtures.importing)
        #expect(job.status == .importing)
        #expect(job.progress.pagesDone == 61)
        #expect(job.progress.current == "Work › Meetings › Weekly sync 2024-03-11")
        #expect(job.progress.fraction.map { abs($0 - 61.0 / 148.0) < 0.0001 } == true)
        #expect(OneNoteImport.pageCount(job.progress) == "61 of 148 pages")
        #expect(OneNoteImport.step(for: job.status) == .importPages)
        #expect(!job.status.isFinished)
    }

    @Test func decodesTheReport() throws {
        let job = try OneNoteFixtures.job(OneNoteFixtures.done)
        #expect(job.status.isFinished)
        let result = try #require(job.result)
        #expect(result.notebooks.map(\.name) == ["Work", "Recipes"])
        let work = result.notebooks[0]
        #expect(work.folderId == "01M7FOLDERWORK000000000000")
        #expect(work.pages == 95)
        #expect(work.pagesSkipped == 1)
        #expect(work.columnsPages == 9)
        #expect(work.warnings == ["Meetings › Kickoff: 1 ink drawing(s) not imported (Fortress does not support ink)",
                                  "Projects › Roadmap: a video link was kept as a link"])
        #expect(work.failedSections == [.init(path: "Work › HR (locked)", reason: "The section is password-protected.",
                                              hint: "Unlock it in OneNote, export the notebook again and re-import.")])
        #expect(result.links.rewritten == 214)
        #expect(result.links.unresolved.count == 2)
        #expect(result.durationMs == 41870)
        #expect(result.errorsTxt?.hasPrefix("Work/HR.one") == true)
        #expect(result.pages == 147)
        #expect(result.problemCount == 3)
        #expect(OneNoteImport.headline(result) == "Imported 2 notebooks")
        #expect(OneNoteImport.summary(result) == "147 pages · 36 images · 9 files · 12 tables · in 42 sec")
    }

    @Test func recognisesAMissingConverter() throws {
        let job = try OneNoteFixtures.job(OneNoteFixtures.failed)
        #expect(job.status == .failed)
        #expect(job.converterMissing)
        #expect(OneNoteImport.isConverterMissing("fortress-onenote is not installed"))
        #expect(!OneNoteImport.isConverterMissing("The file is not a OneNote export"))
        #expect(!OneNoteImport.isConverterMissing("Section not found"))
    }

    @Test func decodesRecentJobsEitherWay() throws {
        let wrapped = try OneNoteImport.decodeRecent(Data(OneNoteFixtures.recent.utf8))
        #expect(wrapped.map(\.jobId) == ["job-7f3a", "job-6e21"])
        #expect(wrapped[0].filename == "Work.zip")
        #expect(wrapped[1].error == "The file is not a OneNote export")
        let bare = try OneNoteImport.decodeRecent(Data("[\(OneNoteFixtures.queued)]".utf8))
        #expect(bare.count == 1)
    }

    @Test func toleratesSparseAndNewerShapes() throws {
        let job = try OneNoteFixtures.job(#"{"job_id": "j", "status": "optimizing", "result": {"notebooks": [{"name": "A"}]}}"#)
        #expect(job.status == .converting)
        #expect(job.result?.notebooks.first?.pages == 0)
        #expect(job.result?.links == .init())
        #expect(OneNoteImport.headline(job.result!) == "1 notebook already up to date")
    }

    @Test func acceptsOneNoteExports() {
        for name in ["Work.zip", "Notebook.ONEPKG", "Section.one"] {
            #expect(OneNoteImport.accepts(URL(fileURLWithPath: "/tmp/\(name)")))
        }
        #expect(!OneNoteImport.accepts(URL(fileURLWithPath: "/tmp/notes.md")))
    }

    @Test func wordsDurations() {
        #expect(OneNoteImport.durationText(400) == "under a second")
        #expect(OneNoteImport.durationText(41_870) == "42 sec")
        #expect(OneNoteImport.durationText(125_000) == "2 min, 5 sec")
        // Counts agree with their noun (automatic grammar agreement, not an appended "s").
        #expect(OneNoteImport.plural(1, "page") == "1 page")
        #expect(OneNoteImport.plural(1_204, "image") == "1,204 images")
        #expect(OneNoteImport.pageCount(.init(pagesTotal: 1, pagesDone: 1, current: nil)) == "1 of 1 page")
    }
}

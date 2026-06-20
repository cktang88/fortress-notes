import type { ConsistencyReport, HealReport } from "./types";

const SEVERITY_COLORS: Record<string, string> = {
  high: "bg-red-100 text-red-700",
  medium: "bg-amber-100 text-amber-700",
  low: "bg-zinc-200 text-zinc-600",
};

function PanelShell({
  title,
  loading,
  onClose,
  children,
}: {
  title: string;
  loading: boolean;
  onClose: () => void;
  children?: React.ReactNode;
}) {
  return (
    <div className="mx-8 mb-8 rounded-lg border border-zinc-200 bg-zinc-50 p-4">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-zinc-700">{title}</h3>
        <button onClick={onClose} className="text-xs text-zinc-400 hover:text-zinc-600">
          ✕
        </button>
      </div>
      {loading ? <p className="text-sm text-zinc-400">Running agentic pass…</p> : children}
    </div>
  );
}

export function ConsistencyPanel({
  report,
  loading,
  onClose,
  onOpenNote,
}: {
  report: ConsistencyReport | null;
  loading: boolean;
  onClose: () => void;
  onOpenNote: (id: string) => void;
}) {
  if (!loading && !report) return null;
  return (
    <PanelShell title="Cross-note consistency" loading={loading} onClose={onClose}>
      {report && (
        <>
          <p className="mb-3 text-sm text-zinc-600">{report.summary}</p>
          {report.issues.length === 0 ? (
            <p className="text-sm text-emerald-600">No contradictions found. ✓</p>
          ) : (
            <ul className="flex flex-col gap-3">
              {report.issues.map((issue, i) => (
                <li key={i} className="rounded-md border border-zinc-200 bg-white p-3 text-sm">
                  <div className="mb-1 flex items-center gap-2">
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] font-medium uppercase ${
                        SEVERITY_COLORS[issue.severity] ?? SEVERITY_COLORS.low
                      }`}
                    >
                      {issue.severity}
                    </span>
                    {issue.related_note_id && (
                      <button
                        onClick={() => onOpenNote(issue.related_note_id)}
                        className="text-xs text-indigo-600 hover:underline"
                      >
                        vs. {issue.related_note_title || "related note"}
                      </button>
                    )}
                  </div>
                  <p className="text-zinc-700">
                    <span className="font-medium">This note:</span> {issue.claim}
                  </p>
                  <p className="text-zinc-700">
                    <span className="font-medium">Conflicts with:</span> {issue.conflict}
                  </p>
                </li>
              ))}
            </ul>
          )}
          {report.checked_against.length > 0 && (
            <p className="mt-3 text-[11px] text-zinc-400">
              Checked against: {report.checked_against.join(", ")}
            </p>
          )}
        </>
      )}
    </PanelShell>
  );
}

export function HealPanel({
  report,
  loading,
  onClose,
}: {
  report: HealReport | null;
  loading: boolean;
  onClose: () => void;
}) {
  if (!loading && !report) return null;
  return (
    <PanelShell title="Self-healing" loading={loading} onClose={onClose}>
      {report && (
        <>
          <p className="mb-3 text-sm text-zinc-600">{report.summary}</p>

          {report.dead_links.length > 0 && (
            <div className="mb-3">
              <h4 className="mb-1 text-xs font-semibold uppercase text-zinc-400">Broken links</h4>
              <ul className="flex flex-col gap-1">
                {report.dead_links.map((d, i) => (
                  <li key={i} className="text-sm">
                    <span className="mr-2 rounded bg-red-100 px-1.5 py-0.5 text-[10px] font-medium text-red-700">
                      {d.status}
                    </span>
                    <span className="break-all text-zinc-700">{d.url}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {report.stale_facts.length > 0 && (
            <div>
              <h4 className="mb-1 text-xs font-semibold uppercase text-zinc-400">
                Possibly stale facts
              </h4>
              <ul className="flex flex-col gap-2">
                {report.stale_facts.map((f, i) => (
                  <li key={i} className="rounded-md border border-zinc-200 bg-white p-3 text-sm">
                    <p className="text-zinc-700">
                      <span className="font-medium">Claim:</span> {f.claim}
                    </p>
                    <p className="text-zinc-700">
                      <span className="font-medium">Found:</span> {f.finding}
                    </p>
                    <p className="text-emerald-700">
                      <span className="font-medium">Suggest:</span> {f.suggestion}
                    </p>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {report.dead_links.length === 0 && report.stale_facts.length === 0 && (
            <p className="text-sm text-emerald-600">Nothing to heal. ✓</p>
          )}
        </>
      )}
    </PanelShell>
  );
}

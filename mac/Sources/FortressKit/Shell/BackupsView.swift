import SwiftUI

/// Backups: back up now, browse snapshots, restore one (after confirming).
/// Shown as a sheet (File → Restore from Backup…) and as a Settings tab.
struct BackupsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss
    var isSheet = false
    @State private var selection: Backup.ID?
    @State private var restoring: Backup?
    @State private var isWorking = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if isSheet {
                Text("Backups")
                    .font(.title2.weight(.semibold))
            }
            Text("Fortress backs up your notes once a day. Restoring saves your current notes as a backup first, so nothing is lost.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            Table(model.backups, selection: $selection) {
                TableColumn("Date") { backup in
                    let date = Timestamp.date(backup.createdAt)
                    Text(date.map { $0.formatted(date: .abbreviated, time: .shortened) } ?? backup.createdAt)
                        .monospacedDigit()
                        .help(date.map { $0.formatted(.relative(presentation: .named)) } ?? "")
                }
                .width(min: 170, ideal: 210)
                TableColumn("Kind") { backup in
                    Text(BackupKind.label(backup.kind))
                        .foregroundStyle(.secondary)
                }
                .width(min: 80, ideal: 100)
                TableColumn("Size") { backup in
                    Text(backup.size.formatted(.byteCount(style: .file)))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
                .width(min: 60, ideal: 80)
            }
            .contextMenu(forSelectionType: Backup.ID.self) { ids in
                if let backup = model.backups.first(where: { ids.contains($0.id) }) {
                    Button("Restore…") { restoring = backup }
                }
            } primaryAction: { ids in
                restoring = model.backups.first { ids.contains($0.id) }
            }
            .frame(minHeight: 140, maxHeight: .infinity)
            .overlay {
                if model.backups.isEmpty {
                    ContentUnavailableView(
                        "No Backups Yet", systemImage: "clock.arrow.circlepath",
                        description: Text("Back up now, or wait for the daily backup."))
                }
            }

            HStack {
                Button {
                    Task {
                        isWorking = true
                        await model.backUpNow()
                        isWorking = false
                    }
                } label: {
                    Label("Back Up Now", systemImage: "clock.arrow.circlepath")
                }
                .disabled(isWorking || !model.isConnected)
                if isWorking { ProgressView().controlSize(.small) }
                Spacer()
                if isSheet {
                    Button("Done") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                }
                Button("Restore…") { restoring = model.backups.first { $0.id == selection } }
                    .disabled(selection == nil || isWorking)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(isSheet ? 20 : 0)
        .task { if model.connectsOnAppear { await model.refreshBackups() } }
        .confirmationDialog(
            restoring.map { "Restore the backup from \(BackupKind.dateLabel($0))?" } ?? "",
            isPresented: Binding(get: { restoring != nil }, set: { if !$0 { restoring = nil } }),
            presenting: restoring
        ) { backup in
            Button("Restore") {
                Task {
                    isWorking = true
                    let restored = await model.restoreBackup(backup)
                    isWorking = false
                    if restored && isSheet { dismiss() }
                }
            }
        } message: { _ in
            Text("Your current notes will be saved as a backup first.")
        }
    }
}

enum BackupKind {
    static func label(_ kind: String) -> String {
        switch kind {
        case "auto": "Daily"
        case "manual": "Manual"
        case "pre-restore": "Before Restore"
        default: kind.capitalized
        }
    }

    static func dateLabel(_ backup: Backup) -> String {
        Timestamp.date(backup.createdAt)?.formatted(date: .abbreviated, time: .shortened) ?? backup.createdAt
    }
}

import SwiftUI

/// Unified search results with highlighted passages. (Finding area — placeholder.)
struct SearchResultsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        ContentUnavailableView.search(text: model.query)
    }
}

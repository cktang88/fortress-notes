import FortressKit
import SwiftUI
import Testing

@MainActor
@Test func placeholderWindowRenders() throws {
    try snapshot(PlaceholderView(), named: "placeholder-light")
    try snapshot(PlaceholderView(), named: "placeholder-dark", appearance: .darkAqua)
}

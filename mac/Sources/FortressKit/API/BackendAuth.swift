import CryptoKit
import Foundation
import Security

/// The secret this launch shares with the backend it starts (backend/app/auth.py).
///
/// The app makes a fresh random token per launch and hands it to the backend process
/// in `FORTRESS_TOKEN`; every request carries it in `X-Fortress-Token`, so web pages
/// (which can reach 127.0.0.1 but can't learn the token, nor send a custom header
/// cross-site without a refused CORS preflight) and other users' processes can't use
/// the API. The backend names itself in `X-Fortress-Instance` (a hash of its token), so
/// the app only ever talks to the backend it started, never to whatever else answers
/// on the port.
public enum BackendAuth {
    public static let tokenHeader = "X-Fortress-Token"
    public static let instanceHeader = "X-Fortress-Instance"
    public static let tokenEnvironment = "FORTRESS_TOKEN"

    /// This launch's token: `FORTRESS_TOKEN` from the app's own environment when set
    /// (CI runs the integration tests against a backend started with a known token),
    /// otherwise 32 random bytes.
    public static let token: String = {
        if let given = ProcessInfo.processInfo.environment[tokenEnvironment], !given.isEmpty { return given }
        var bytes = [UInt8](repeating: 0, count: 32)
        if SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) != errSecSuccess {
            var generator = SystemRandomNumberGenerator()
            bytes = bytes.map { _ in UInt8.random(in: .min ... .max, using: &generator) }
        }
        return bytes.map { String(format: "%02x", $0) }.joined()
    }()

    /// What a backend started with `token` says in `X-Fortress-Instance`: the first 16
    /// hex digits of the token's SHA-256.
    public static func instance(for token: String) -> String {
        SHA256.hash(data: Data(token.utf8)).map { String(format: "%02x", $0) }.joined().prefix(16).description
    }

    /// The instance id of the backend this launch starts.
    public static var instanceID: String { instance(for: token) }

    /// Headers every request to the backend carries (set on the URLSession configurations).
    public static var headers: [String: String] { [tokenHeader: token] }
}

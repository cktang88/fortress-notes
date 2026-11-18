import Foundation

/// A small least-recently-used cache. Not thread-safe: each owner keeps it on its actor
/// (Finding's controllers are all on the main actor).
struct LRUCache<Key: Hashable, Value> {
    let capacity: Int
    private var values: [Key: Value] = [:]
    /// Keys, least recently used first. Capacities are small (≤ a few hundred), so the
    /// linear reorder is cheaper than a linked list's bookkeeping.
    private var order: [Key] = []

    init(capacity: Int) {
        precondition(capacity > 0)
        self.capacity = capacity
    }

    var count: Int { values.count }

    /// The value, marking it most recently used.
    mutating func value(for key: Key) -> Value? {
        guard let value = values[key] else { return nil }
        touch(key)
        return value
    }

    /// The value without changing its place.
    func peek(_ key: Key) -> Value? { values[key] }

    mutating func insert(_ value: Value, for key: Key) {
        if values.updateValue(value, forKey: key) != nil {
            touch(key)
        } else {
            order.append(key)
            if order.count > capacity {
                values[order.removeFirst()] = nil
            }
        }
    }

    mutating func remove(_ key: Key) {
        guard values.removeValue(forKey: key) != nil else { return }
        order.removeAll { $0 == key }
    }

    mutating func removeAll() {
        values = [:]
        order = []
    }

    private mutating func touch(_ key: Key) {
        if order.last == key { return }
        if let index = order.lastIndex(of: key) { order.remove(at: index) }
        order.append(key)
    }
}

/// An LRU cache whose entries are forgotten after `lifetime` seconds.
struct ExpiringCache<Key: Hashable, Value> {
    private var storage: LRUCache<Key, (value: Value, stored: Date)>
    let lifetime: TimeInterval

    init(capacity: Int, lifetime: TimeInterval) {
        storage = LRUCache(capacity: capacity)
        self.lifetime = lifetime
    }

    mutating func value(for key: Key, now: Date) -> Value? {
        guard let entry = storage.value(for: key) else { return nil }
        guard now.timeIntervalSince(entry.stored) < lifetime else {
            storage.remove(key)
            return nil
        }
        return entry.value
    }

    mutating func insert(_ value: Value, for key: Key, now: Date) {
        storage.insert((value, now), for: key)
    }

    mutating func removeAll() { storage.removeAll() }
}

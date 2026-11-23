use std::collections::{HashMap, VecDeque};
use std::hash::Hash;
use std::time::{Duration, Instant};

pub struct TtlCache<K, V> {
    ttl: Duration,
    max_size: usize,
    entries: HashMap<K, (V, Instant)>,
    order: VecDeque<K>,
}

impl<K: Eq + Hash + Clone, V: Clone> TtlCache<K, V> {
    pub fn new(ttl: Duration, max_size: usize) -> Self {
        Self {
            ttl,
            max_size,
            entries: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    pub fn get(&self, key: &K) -> Option<V> {
        let (val, inserted_at) = self.entries.get(key)?;
        if inserted_at.elapsed() < self.ttl {
            Some(val.clone())
        } else {
            None
        }
    }

    pub fn get_stale(&self, key: &K) -> Option<V> {
        self.entries.get(key).map(|(v, _)| v.clone())
    }

    pub fn insert(&mut self, key: K, value: V) {
        let now = Instant::now();
        self.entries.insert(key.clone(), (value, now));
        self.order.push_back(key);
        if self.entries.len() > self.max_size || self.order.len() > self.max_size * 2 {
            self.prune(now);
        }
    }

    pub fn remove(&mut self, key: &K) -> Option<V> {
        self.entries.remove(key).map(|(v, _)| v)
    }

    fn prune(&mut self, now: Instant) {
        let ttl = self.ttl;
        self.entries
            .retain(|_, (_, ts)| now.saturating_duration_since(*ts) < ttl);
        while self.entries.len() > self.max_size {
            if let Some(oldest) = self.order.pop_front() {
                self.entries.remove(&oldest);
            } else {
                break;
            }
        }
        if self.order.len() > self.max_size * 2 {
            self.order.retain(|k| self.entries.contains_key(k));
        }
    }
}

pub struct BoundedMap<K, V> {
    max_size: usize,
    entries: HashMap<K, V>,
    order: VecDeque<K>,
}

impl<K: Eq + Hash + Clone, V: Clone> BoundedMap<K, V> {
    pub fn new(max_size: usize) -> Self {
        Self {
            max_size,
            entries: HashMap::new(),
            order: VecDeque::new(),
        }
    }

    pub fn get(&self, key: &K) -> Option<V> {
        self.entries.get(key).cloned()
    }

    pub fn insert(&mut self, key: K, value: V) {
        self.entries.insert(key.clone(), value);
        self.order.push_back(key);
        while self.entries.len() > self.max_size {
            if let Some(oldest) = self.order.pop_front() {
                self.entries.remove(&oldest);
            } else {
                break;
            }
        }
        if self.order.len() > self.max_size * 2 {
            self.order.retain(|k| self.entries.contains_key(k));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ttl_cache_evicts_by_capacity_and_expires() {
        let mut cache = TtlCache::new(Duration::from_secs(60), 2);
        cache.insert("a", 1);
        cache.insert("b", 2);
        cache.insert("c", 3);
        assert_eq!(cache.get(&"a"), None);
        assert_eq!(cache.get(&"b"), Some(2));
        assert_eq!(cache.get(&"c"), Some(3));
        assert_eq!(cache.remove(&"b"), Some(2));
        assert_eq!(cache.get(&"b"), None);

        let mut instant_expire = TtlCache::new(Duration::from_secs(0), 4);
        instant_expire.insert("x", 42);
        assert_eq!(instant_expire.get(&"x"), None);
        assert_eq!(instant_expire.get_stale(&"x"), Some(42));
    }

    #[test]
    fn bounded_map_caps_entries() {
        let mut map = BoundedMap::new(3);
        for i in 0..5 {
            map.insert(i, i * 10);
        }
        assert_eq!(map.get(&0), None);
        assert_eq!(map.get(&1), None);
        assert_eq!(map.get(&2), Some(20));
        assert_eq!(map.get(&4), Some(40));
    }
}

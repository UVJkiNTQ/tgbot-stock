use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Default)]
pub struct MaintenanceGate {
    active: AtomicBool,
}

impl MaintenanceGate {
    pub fn new() -> Self {
        Self {
            active: AtomicBool::new(false),
        }
    }

    pub fn is_active(&self) -> bool {
        self.active.load(Ordering::SeqCst)
    }

    /// Enter maintenance mode once; returns false if already active.
    pub fn begin(&self) -> bool {
        self.active
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_ok()
    }

    pub fn end(&self) {
        self.active.store(false, Ordering::SeqCst);
    }

    pub fn is_update_command(text: Option<&str>) -> bool {
        let Some(t) = text else {
            return false;
        };
        let Some(first) = t.split_whitespace().next() else {
            return false;
        };
        let cmd = first.split('@').next().unwrap_or("");
        cmd.eq_ignore_ascii_case("/update")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn maintenance_gate_transitions() {
        let gate = MaintenanceGate::new();
        assert!(!gate.is_active());
        assert!(gate.begin());
        assert!(!gate.begin());
        assert!(gate.is_active());
        gate.end();
        assert!(!gate.is_active());
        assert!(MaintenanceGate::is_update_command(Some("/update@stock_bot")));
        assert!(!MaintenanceGate::is_update_command(Some("/position")));
    }
}

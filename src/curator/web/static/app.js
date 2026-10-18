function curatorApp() {
    return {
        groups: [],
        selectedId: null,
        showShortcuts: false,
        showHintBar: false,
        undoStack: [],

        init() {
            const data = JSON.parse(document.getElementById('initial-data').textContent);
            this.groups = data.groups;
            if (this.groups.length > 0) {
                const pending = this.groups.find(g => g.status === 'pending') || this.groups[0];
                this.select(pending.id);
            }
            if (!localStorage.getItem('curator-hint-dismissed')) {
                this.showHintBar = true;
                setTimeout(() => { this.showHintBar = false; }, 8000);
            }

            this.pollAnalysis();
        },

        /* ---------- Getters ---------- */
        get selectedGroup() {
            return this.groups.find(g => g.id === this.selectedId) || null;
        },
        get currentWinner() {
            const g = this.selectedGroup;
            if (!g) return null;
            return g.photos.find(p => p.id === g.current_winner) || null;
        },
        get rejectedPhotos() {
            const g = this.selectedGroup;
            if (!g) return [];
            return g.photos.filter(p => p.id !== g.current_winner && p.status !== 'deleted');
        },
        get pendingCount() {
            return this.groups.filter(g => g.status === 'pending').length;
        },

        /* ---------- Helpers ---------- */
        winnerThumb(group) {
            const w = group.photos.find(p => p.id === group.current_winner);
            return w ? w.thumb : '';
        },
        formatDate(iso) {
            const d = new Date(iso);
            const now = new Date();
            const sameDay = d.toDateString() === now.toDateString();
            if (sameDay) return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
            return d.toLocaleDateString('en-US', { day: 'numeric', month: 'short' });
        },

        /* ---------- Actions ---------- */
        select(id) {
            this.selectedId = id;
            this.$nextTick(() => {
                const el = this.$refs['group-' + id];
                if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            });
        },

        openLightbox(photoId) {
            const g = this.selectedGroup;
            if (!g) return;
            const photos = g.photos.filter(p => p.status !== 'deleted');
            const idx = photos.findIndex(p => p.id === photoId);
            if (idx === -1) return;
            this.$dispatch('open-lightbox', { photos, index: idx });
        },

        promotePhoto(photoId) {
            const g = this.selectedGroup;
            if (!g || g.status === 'deleted') return;
            const incoming = g.photos.find(p => p.id === photoId);
            if (!incoming) return;
            const prevWinnerId = g.current_winner;
            const prevStatus = g.status;
            const outgoing = g.photos.find(p => p.id === prevWinnerId);
            if (outgoing) outgoing.status = 'rejected';
            incoming.status = 'winner';
            g.current_winner = incoming.id;
            if (g.status === 'completed') g.status = 'pending';
            this.pushUndo(() => {
                if (incoming.status === 'winner') incoming.status = 'rejected';
                if (outgoing) outgoing.status = 'winner';
                g.current_winner = prevWinnerId;
                g.status = prevStatus;
            });
        },

        confirmGroup() {
            const g = this.selectedGroup;
            if (!g || g.status !== 'pending') return;
            const prevStatus = g.status;
            g.status = 'completed';
            this.pushUndo(() => { g.status = prevStatus; });
            this.advanceToNextPending();
        },

        deleteGroup() {
            const g = this.selectedGroup;
            if (!g || g.status === 'deleted') return;
            const prevStatus = g.status;
            g.status = 'deleted';
            this.pushUndo(() => { g.status = prevStatus; });
            this.toast(
                `Group ${g.id} deleted — photos still on disk`,
                'Undo',
                () => { g.status = prevStatus; this.selectedId = g.id; }
            );
            this.advanceToNextPending();
        },

        restoreGroup() {
            const g = this.selectedGroup;
            if (!g || g.status !== 'deleted') return;
            g.status = 'pending';
        },

        advanceToNextPending() {
            const idx = this.groups.findIndex(g => g.id === this.selectedId);
            const order = [...this.groups.slice(idx + 1), ...this.groups.slice(0, idx)];
            const next = order.find(g => g.status === 'pending');
            if (next) {
                this.select(next.id);
            } else if (this.pendingCount === 0) {
                this.toast('All groups reviewed', null, null);
            }
        },

        navigate(delta) {
            const idx = this.groups.findIndex(g => g.id === this.selectedId);
            if (idx === -1) {
                this.select(this.groups[0].id);
                return;
            }
            const next = Math.max(0, Math.min(this.groups.length - 1, idx + delta));
            this.select(this.groups[next].id);
        },

        /* ---------- Undo ---------- */
        pushUndo(fn) {
            this.undoStack.push(fn);
            if (this.undoStack.length > 30) this.undoStack.shift();
        },
        undo() {
            const fn = this.undoStack.pop();
            if (fn) {
                fn();
                this.toast('Undone', null, null);
            }
        },

        /* ---------- Keyboard ---------- */
        onKey(e) {
            const tag = (e.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;

            if (e.key === 'Escape') {
                if (this.showShortcuts) { this.showShortcuts = false; e.preventDefault(); return; }
                this.selectedId = null;
                e.preventDefault();
                return;
            }
            if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z')) {
                this.undo(); e.preventDefault(); return;
            }
            if (e.metaKey || e.ctrlKey || e.altKey) return;

            if (e.key === '?') { this.showShortcuts = !this.showShortcuts; e.preventDefault(); return; }
            if (this.showShortcuts) return;

            if (e.key === 'j' || e.key === 'ArrowDown') { this.navigate(1);  e.preventDefault(); return; }
            if (e.key === 'k' || e.key === 'ArrowUp')   { this.navigate(-1); e.preventDefault(); return; }
            if (e.key === 'Enter') { this.confirmGroup(); e.preventDefault(); return; }
            if (e.key === 'Backspace' || e.key === 'Delete') { this.deleteGroup(); e.preventDefault(); return; }
            if (/^[1-9]$/.test(e.key)) {
                const photo = this.rejectedPhotos[parseInt(e.key, 10) - 1];
                if (photo) { this.promotePhoto(photo.id); e.preventDefault(); }
            }
        },

        /* ---------- Toast ---------- */
        toast(text, actionLabel, onAction) {
            this.$refs.toasts.querySelectorAll('.toast').forEach(t => this.dismissToast(t));
            const el = document.createElement('div');
            el.className = 'toast';
            const txt = document.createElement('span');
            txt.className = 'toast__text';
            txt.textContent = text;
            el.appendChild(txt);
            if (actionLabel) {
                const btn = document.createElement('button');
                btn.type = 'button';
                btn.className = 'toast__action';
                btn.textContent = actionLabel;
                btn.onclick = () => { if (onAction) onAction(); this.dismissToast(el); };
                el.appendChild(btn);
            }
            this.$refs.toasts.appendChild(el);
            el._timeout = setTimeout(() => this.dismissToast(el), actionLabel ? 5000 : 1800);
        },
        dismissToast(el) {
            if (!el || !el.parentNode) return;
            clearTimeout(el._timeout);
            el.classList.add('toast--out');
            setTimeout(() => el.remove(), 200);
        },

        /* ---------- Hint bar ---------- */
        dismissHint() {
            this.showHintBar = false;
            localStorage.setItem('curator-hint-dismissed', '1');
        },

        // --- Progress ring ---
        get completedCount() {
            return this.groups.filter(g => g.status === 'completed' || g.status === 'deleted').length;
        },
        get ringPercent() {
            if (this.groups.length === 0) return 0;
            return Math.round((this.completedCount / this.groups.length) * 100);
        },
        get ringOffset() {
            // 2πr con r=16 → circonferenza ≈ 100.53
            return (100.53 * (1 - this.ringPercent / 100)).toFixed(2);
        },

        // --- Analysis polling (Sprint 1: endpoint simulate; Sprint 3: same shape) ---
        analysis: { running: false, progress: null, current_task: null, error: null },
        analysisPoller: null,
        pollAnalysis() {
            if (this.analysisPoller) return;
            const tick = async () => {
                try {
                    const r = await fetch('/api/analyze/status');
                    this.analysis = await r.json();
                    if (!this.analysis.running) {
                        clearInterval(this.analysisPoller);
                        this.analysisPoller = null;
                    }
                } catch {}
            };
            tick();
            this.analysisPoller = setInterval(tick, 2000);
        },
        startFakeAnalysis() {
            fetch('/api/analyze/simulate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ total: 318 }),
            }).then(() => this.pollAnalysis());
        },

    };
}

function lightbox() {
    return {
        open: false,
        photos: [],
        index: 0,
        zoom: false,
        zoomX: 50, zoomY: 50,

        show(photos, startIndex = 0) {
            this.photos = photos;
            this.index = startIndex;
            this.open = true;
            this.zoom = false;
            document.body.style.overflow = 'hidden';
        },
        close() {
            this.open = false;
            this.zoom = false;
            document.body.style.overflow = '';
        },
        next() { if (this.index < this.photos.length - 1) { this.index++; this.zoom = false; } },
        prev() { if (this.index > 0) { this.index--; this.zoom = false; } },
        toggleZoom(e) {
            if (!this.zoom) {
                const rect = e.currentTarget.getBoundingClientRect();
                this.zoomX = ((e.clientX - rect.left) / rect.width) * 100;
                this.zoomY = ((e.clientY - rect.top) / rect.height) * 100;
                this.zoom = true;
            } else {
                this.zoom = false;
            }
        },
        get current() { return this.photos[this.index]; },
        get transform() {
            return this.zoom
                ? `scale(2.5) translate(${50 - this.zoomX}%, ${50 - this.zoomY}%)`
                : 'scale(1)';
        },
    };
}
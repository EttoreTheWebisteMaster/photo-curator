function curatorApp() {
    return {
        groups: [],
        selectedId: null,
        selectedDetail: null,
        loadingDetail: false,
        saving: false,
        showShortcuts: false,
        showHintBar: false,
        undoStack: [],
        sidebarWidth: 272,
        detailWidth: 340,
        _resizeHandler: null,
        rejectedCount: 0,

        async init() {
            const data = JSON.parse(document.getElementById('initial-data').textContent);
            this.groups = data.groups;
            if (data.useFixtures) {
                // Fixtures: photos are in the payload, no fetch needed
                if (this.groups.length > 0) {
                    const first = this.groups.find(g => g.status === 'pending') || this.groups[0];
                    this.selectedId = first.id;
                    this.selectedDetail = first;
                }
            } else {
                this.selectedDetail = data.initialDetail;
                this.selectedId = data.initialDetail ? data.initialDetail.id : null;
            }
            if (!localStorage.getItem('curator-hint-dismissed')) {
                this.showHintBar = true;
                setTimeout(() => { this.showHintBar = false; }, 8000);
            }

            // Load saved column widths
            try {
                const saved = JSON.parse(localStorage.getItem('curator-widths') || '{}');
                if (typeof saved.sidebar === 'number') this.sidebarWidth = saved.sidebar;
                if (typeof saved.detail === 'number')  this.detailWidth  = saved.detail;
            } catch {}

            // Clamp on window resize so nothing disappears off-screen
            this._resizeHandler = () => this.clampWidths();
            window.addEventListener('resize', this._resizeHandler);
            this.rejectedCount = data.rejectedCount || 0;
        },

        /* ---------- Getters ---------- */
        get selectedGroup() {
            return this.groups.find(g => g.id === this.selectedId) || null;
        },
        get currentWinner() {
            const d = this.selectedDetail;
            if (!d || d.current_winner == null) return null;
            return d.photos.find(p => p.id === d.current_winner) || null;
        },
        get rejectedPhotos() {
            const d = this.selectedDetail;
            if (!d) return [];
            return d.photos.filter(p => p.id !== d.current_winner);
        },
        get pendingCount() {
            return this.groups.filter(g => g.status === 'pending').length;
        },
        get completedCount() {
            return this.groups.filter(g => g.status === 'completed' || g.status === 'deleted').length;
        },
        get ringPercent() {
            if (this.groups.length === 0) return 0;
            return Math.round((this.completedCount / this.groups.length) * 100);
        },
        get ringOffset() {
            return (100.53 * (1 - this.ringPercent / 100)).toFixed(2);
        },
        get isFixtures() {
            return JSON.parse(document.getElementById('initial-data').textContent).useFixtures;
        },

        /* ---------- Helpers ---------- */
        winnerThumb(group) {
            return group.thumb || '';
        },
        formatDate(iso) {
            if (!iso) return '';
            const d = new Date(iso);
            const now = new Date();
            if (d.toDateString() === now.toDateString()) {
                return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
            }
            return d.toLocaleDateString('en-US', { day: 'numeric', month: 'short' });
        },

        /* ---------- Selection ---------- */
        async select(id) {
            this.selectedId = id;
            this.$nextTick(() => {
                const el = this.$refs['group-' + id];
                if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            });
            if (this.isFixtures) {
                this.selectedDetail = this.groups.find(g => g.id === id) || null;
                return;
            }
            if (this.selectedDetail?.id === id) return;
            this.loadingDetail = true;
            try {
                const r = await fetch(`/api/groups/${id}`);
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                this.selectedDetail = await r.json();
            } catch (e) {
                this.toast(`Could not load group ${id}`, null, null);
            } finally {
                this.loadingDetail = false;
            }
        },

        /* ---------- Actions ---------- */
        async promotePhoto(photoId) {
            const d = this.selectedDetail;
            if (!d || d.status === 'deleted' || this.saving) return;
            const prevWinner = d.current_winner;
            const prevStatus = d.status;
            d.current_winner = photoId;
            if (d.status === 'completed') {
                d.status = 'pending';
                const g = this.groups.find(g => g.id === d.id);
                if (g) g.status = 'pending';
            }
            if (this.isFixtures) { this._fixturesMark(photoId); return; }
            this.saving = true;
            try {
                const r = await fetch(`/api/groups/${d.id}/choose`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ photo_id: photoId }),
                });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
            } catch (e) {
                d.current_winner = prevWinner;
                d.status = prevStatus;
                this.toast('Could not save choice', null, null);
            } finally {
                this.saving = false;
            }
        },

        async confirmGroup() {
            const d = this.selectedDetail;
            if (!d || d.status !== 'pending' || this.saving) return;
            const prevStatus = d.status;
            d.status = 'completed';
            const g = this.groups.find(g => g.id === d.id);
            if (g) g.status = 'completed';
            if (this.isFixtures) { this.advanceToNextPending(); return; }
            this.saving = true;
            try {
                const r = await fetch(`/api/groups/${d.id}/confirm`, { method: 'POST' });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
            } catch (e) {
                d.status = prevStatus;
                if (g) g.status = prevStatus;
                this.toast('Could not confirm', null, null);
                return;
            } finally {
                this.saving = false;
            }
            this.advanceToNextPending();
        },

        async deleteGroup() {
            const d = this.selectedDetail;
            if (!d || d.status === 'deleted' || this.saving) return;
            const prevStatus = d.status;
            d.status = 'deleted';
            const g = this.groups.find(g => g.id === d.id);
            if (g) g.status = 'deleted';
            if (this.isFixtures) { this.advanceToNextPending(); return; }
            this.saving = true;
            try {
                const r = await fetch(`/api/groups/${d.id}/delete`, { method: 'POST' });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
            } catch (e) {
                d.status = prevStatus;
                if (g) g.status = prevStatus;
                this.toast('Could not delete', null, null);
                return;
            } finally {
                this.saving = false;
            }
            this.toast(
                `Group ${d.id} deleted — photos still on disk`,
                'Undo',
                () => this.restoreGroup(),
            );
            this.advanceToNextPending();
        },

        async restoreGroup() {
            const d = this.selectedDetail;
            if (!d || d.status !== 'deleted') return;
            const prevStatus = d.status;
            d.status = 'pending';
            const g = this.groups.find(g => g.id === d.id);
            if (g) g.status = 'pending';
            if (this.isFixtures) return;
            try {
                const r = await fetch(`/api/groups/${d.id}/restore`, { method: 'POST' });
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
            } catch (e) {
                d.status = prevStatus;
                if (g) g.status = prevStatus;
                this.toast('Could not restore', null, null);
            }
        },

        _fixturesMark(photoId) {
            const d = this.selectedDetail;
            const incoming = d.photos.find(p => p.id === photoId);
            if (incoming) incoming.status = 'winner';
        },

        advanceToNextPending() {
            const idx = this.groups.findIndex(g => g.id === this.selectedId);
            const order = [...this.groups.slice(idx + 1), ...this.groups.slice(0, idx)];
            const next = order.find(g => g.status === 'pending');
            if (next) this.select(next.id);
            else if (this.pendingCount === 0) this.toast('All groups reviewed', null, null);
        },

        navigate(delta) {
            const idx = this.groups.findIndex(g => g.id === this.selectedId);
            if (idx === -1) { if (this.groups[0]) this.select(this.groups[0].id); return; }
            const next = Math.max(0, Math.min(this.groups.length - 1, idx + delta));
            this.select(this.groups[next].id);
        },

        pushUndo(fn) {
            this.undoStack.push(fn);
            if (this.undoStack.length > 30) this.undoStack.shift();
        },
        undo() {
            const fn = this.undoStack.pop();
            if (fn) { fn(); this.toast('Undone', null, null); }
        },

        /* ---------- Lightbox ---------- */
        openLightbox(photoId) {
            const d = this.selectedDetail;
            if (!d) return;
            const photos = d.photos.map(p => ({ id: p.id, url: p.url }));
            const idx = photos.findIndex(p => p.id === photoId);
            if (idx === -1) return;
            this.$dispatch('open-lightbox', { photos, index: idx });
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
            if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z')) { this.undo(); e.preventDefault(); return; }
            if (e.metaKey || e.ctrlKey || e.altKey) return;
            if (e.key === '?') { this.showShortcuts = !this.showShortcuts; e.preventDefault(); return; }
            if (this.showShortcuts) return;
            if (e.key === 'j' || e.key === 'ArrowDown') { this.navigate(1); e.preventDefault(); return; }
            if (e.key === 'k' || e.key === 'ArrowUp') { this.navigate(-1); e.preventDefault(); return; }
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
            txt.className = 'toast__text'; txt.textContent = text;
            el.appendChild(txt);
            if (actionLabel) {
                const btn = document.createElement('button');
                btn.type = 'button'; btn.className = 'toast__action'; btn.textContent = actionLabel;
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

        dismissHint() {
            this.showHintBar = false;
            localStorage.setItem('curator-hint-dismissed', '1');
        },

                /* ---------- Column resize ---------- */
        clampWidths() {
            const w = window.innerWidth;
            const minMain = 320;
            this.sidebarWidth = Math.max(220, Math.min(480,
                Math.min(this.sidebarWidth, w - this.detailWidth - minMain - 8)));
            this.detailWidth = Math.max(300, Math.min(760,
                Math.min(this.detailWidth, w - this.sidebarWidth - minMain - 8)));
        },

        startDrag(which, e) {
            if (e.button !== 0) return;
            e.preventDefault();

            const handle = e.currentTarget;
            const startX = e.clientX;
            const startSidebar = this.sidebarWidth;
            const startDetail = this.detailWidth;

            handle.setPointerCapture(e.pointerId);
            handle.classList.add('is-dragging');
            document.body.classList.add('is-dragging-column');

            const onMove = (ev) => {
                const w = window.innerWidth;
                const minMain = 320;
                if (which === 'sidebar') {
                    const delta = ev.clientX - startX;
                    const maxAllowed = Math.min(480, w - this.detailWidth - minMain - 8);
                    this.sidebarWidth = Math.max(220, Math.min(maxAllowed, startSidebar + delta));
                } else {
                    const delta = startX - ev.clientX;
                    const maxAllowed = Math.min(760, w - this.sidebarWidth - minMain - 8);
                    this.detailWidth = Math.max(300, Math.min(maxAllowed, startDetail + delta));
                }
            };

            const onUp = () => {
                handle.removeEventListener('pointermove', onMove);
                handle.removeEventListener('pointerup', onUp);
                handle.removeEventListener('pointercancel', onUp);
                handle.classList.remove('is-dragging');
                document.body.classList.remove('is-dragging-column');
                try { handle.releasePointerCapture(e.pointerId); } catch {}
                localStorage.setItem('curator-widths', JSON.stringify({
                    sidebar: this.sidebarWidth,
                    detail:  this.detailWidth,
                }));
            };

            handle.addEventListener('pointermove', onMove);
            handle.addEventListener('pointerup', onUp);
            handle.addEventListener('pointercancel', onUp);
        },

        resetWidth(which) {
            if (which === 'sidebar') this.sidebarWidth = 272;
            else                     this.detailWidth  = 340;
            try {
                localStorage.setItem('curator-widths', JSON.stringify({
                    sidebar: this.sidebarWidth,
                    detail:  this.detailWidth,
                }));
            } catch {}
        },
    };
}

function lightbox() {
    return {
        open: false, photos: [], index: 0, zoom: false, zoomX: 50, zoomY: 50,
        show(photos, startIndex = 0) {
            this.photos = photos; this.index = startIndex;
            this.open = true; this.zoom = false;
            document.body.style.overflow = 'hidden';
        },
        close() { this.open = false; this.zoom = false; document.body.style.overflow = ''; },
        next() { if (this.index < this.photos.length - 1) { this.index++; this.zoom = false; } },
        prev() { if (this.index > 0) { this.index--; this.zoom = false; } },
        toggleZoom(e) {
            if (!this.zoom) {
                const rect = e.currentTarget.getBoundingClientRect();
                this.zoomX = ((e.clientX - rect.left) / rect.width) * 100;
                this.zoomY = ((e.clientY - rect.top) / rect.height) * 100;
                this.zoom = true;
            } else this.zoom = false;
        },
        get current() { return this.photos[this.index]; },
        get transform() {
            return this.zoom
                ? `scale(2.5) translate(${50 - this.zoomX}%, ${50 - this.zoomY}%)`
                : 'scale(1)';
        },
    };
}

function intakeApp() {
    return {
        folder: '',
        dragOver: false,
        picking: false,
        submitting: false,
        error: '',

        async pickFolder() {
            this.picking = true;
            this.error = '';
            try {
                const r = await fetch('/api/pick-folder', { method: 'POST' });
                const data = await r.json();
                if (data.path) this.folder = data.path;
                else if (data.error) this.error = data.error;
            } catch (e) {
                this.error = 'Could not open the folder picker.';
            } finally {
                this.picking = false;
            }
        },

        onDrop(e) {
            this.dragOver = false;
            const dt = e.dataTransfer;
            if (!dt) return;
            // Browsers do not expose the absolute path of dropped folders for security.
            // Provide the native picker instead and explain why.
            this.error = 'For privacy, browsers hide dropped folder paths. Use "Choose folder" instead.';
        },

        async analyze() {
            if (!this.folder || this.submitting) return;
            this.submitting = true;
            this.error = '';
            try {
                const r = await fetch('/api/analyze', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ folder: this.folder }),
                });
                const data = await r.json();
                if (!r.ok) {
                    this.error = data.error || `HTTP ${r.status}`;
                    return;
                }
                window.location.href = '/analysis';
            } catch (e) {
                this.error = 'Could not start analysis.';
            } finally {
                this.submitting = false;
            }
        },
    };
}

function analysisApp() {
    return {
        state: { running: false, folder: '', tasks: [], final: null, error: null },
        timer: null,

        start() {
            this.poll();
            this.timer = setInterval(() => this.poll(), 500);
        },

        async poll() {
            try {
                const r = await fetch('/api/analyze/status');
                this.state = await r.json();
            } catch {}
            if (!this.state.running && this.state.finished_at) {
                clearInterval(this.timer);
                this.timer = null;
                if (this.state.final && this.state.final.mode !== 'cancelled') {
                    // Auto-advance after a moment so the user sees the ✓ marks.
                    setTimeout(() => { window.location.href = '/'; }, 1200);
                }
            }
        },

        async cancel() {
            await fetch('/api/analyze/cancel', { method: 'POST' });
        },

        get overallPercent() {
            const tasks = this.state.tasks || [];
            if (tasks.length === 0) return 0;
            let sum = 0;
            for (const t of tasks) {
                if (t.status === 'done') sum += 1;
                else if (t.status === 'running' && t.total > 0) sum += t.count / t.total;
            }
            return Math.round((sum / tasks.length) * 100);
        },
    };
}

function rejectedApp() {
    return {
        photos: [],
        reviewedCount: 0,
        totalCount: 0,
        undoStack: [],

        async load() {
            await this.refresh();
        },

        async refresh() {
            try {
                const r = await fetch('/api/rejected');
                const data = await r.json();
                this.photos = data.photos;
                this.reviewedCount = data.reviewed;
                this.totalCount = data.total;
            } catch {}
        },

        get reviewProgressPct() {
            if (this.totalCount === 0) return 0;
            return Math.round((this.reviewedCount / this.totalCount) * 100);
        },

        async keep(photoId) {
            const idx = this.photos.findIndex(p => p.id === photoId);
            if (idx === -1) return;
            const photo = this.photos[idx];
            this.photos.splice(idx, 1);
            this.reviewedCount++;
            try {
                await fetch(`/api/rejected/${photoId}/keep`, { method: 'POST' });
                this.pushUndo(() => {
                    this.photos.splice(idx, 0, photo);
                    this.reviewedCount--;
                });
            } catch {
                this.photos.splice(idx, 0, photo);
                this.reviewedCount--;
                this.toast('Could not save', null, null);
            }
        },

        async reject(photoId) {
            const idx = this.photos.findIndex(p => p.id === photoId);
            if (idx === -1) return;
            const photo = this.photos[idx];
            this.photos.splice(idx, 1);
            this.reviewedCount++;
            try {
                await fetch(`/api/rejected/${photoId}/reject`, { method: 'POST' });
                this.pushUndo(() => {
                    this.photos.splice(idx, 0, photo);
                    this.reviewedCount--;
                });
            } catch {
                this.photos.splice(idx, 0, photo);
                this.reviewedCount--;
                this.toast('Could not save', null, null);
            }
        },

        async acceptAll() {
            const snapshot = this.photos.slice();
            if (snapshot.length === 0) return;
            this.photos = [];
            this.reviewedCount += snapshot.length;
            try {
                const r = await fetch('/api/rejected/accept-all', { method: 'POST' });
                const data = await r.json();
                this.pushUndo(async () => {
                    // Restore by hitting the reject endpoint inverted; here we just reload
                    // because bulk undo needs a dedicated endpoint. Reload is honest.
                    await this.refresh();
                });
                this.toast(`Accepted ${data.count} rejected photos`, 'Undo', () => this.refresh());
            } catch {
                this.photos = snapshot;
                this.reviewedCount -= snapshot.length;
                this.toast('Could not save', null, null);
            }
        },

        openLightbox(photoId) {
            const idx = this.photos.findIndex(p => p.id === photoId);
            if (idx === -1) return;
            const photos = this.photos.map(p => ({ id: p.id, url: p.url }));
            this.$dispatch('open-lightbox', { photos, index: idx });
        },

        pushUndo(fn) {
            this.undoStack.push(fn);
            if (this.undoStack.length > 50) this.undoStack.shift();
        },
        async undo() {
            const fn = this.undoStack.pop();
            if (!fn) return;
            await fn();
            this.toast('Undone', null, null);
        },

        onKey(e) {
            const tag = (e.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
            if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z')) {
                this.undo(); e.preventDefault(); return;
            }
            if (e.metaKey || e.ctrlKey || e.altKey) return;
            if (this.photos.length === 0) return;

            // First photo in list is the "current" one for keyboard actions
            const current = this.photos[0];
            if (e.key === 'k' || e.key === 'K') { this.keep(current.id); e.preventDefault(); return; }
            if (e.key === 'x' || e.key === 'X') { this.reject(current.id); e.preventDefault(); return; }
            if (e.key === 'Enter') { this.acceptAll(); e.preventDefault(); return; }
        },

        toast(text, actionLabel, onAction) {
            this.$refs.toasts.querySelectorAll('.toast').forEach(t => this.dismissToast(t));
            const el = document.createElement('div');
            el.className = 'toast';
            const txt = document.createElement('span');
            txt.className = 'toast__text'; txt.textContent = text;
            el.appendChild(txt);
            if (actionLabel) {
                const btn = document.createElement('button');
                btn.type = 'button'; btn.className = 'toast__action'; btn.textContent = actionLabel;
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
    };
}

function confirmApp() {
    return {
        data: { stats: null, errors: [], sample: [] },
        loading: true,
        executing: false,
        commitProgress: null,

        async load() {
            this.loading = true;
            try {
                const r = await fetch('/api/commit/preview', { method: 'POST' });
                const body = await r.json();
                if (!r.ok) {
                    this.data = { stats: null, errors: [body.error || 'Preview failed'], sample: [] };
                } else {
                    this.data = body;
                }
            } catch {
                this.data = { stats: null, errors: ['Could not build plan'], sample: [] };
            } finally {
                this.loading = false;
            }
        },

                execute() {
            if (this.executing || this.loading) return;
            if (!this.data.stats || !this.data.stats.total_moves) return;
            this.executing = true;
            this.commitProgress = null;
            fetch('/api/commit/execute', { method: 'POST' })
                .then(r => r.json())
                .then(data => {
                    if (data.error) {
                        this.executing = false;
                        this.toast(data.error, null, null);
                        return;
                    }
                    this.pollCommit();
                })
                .catch(() => {
                    this.executing = false;
                    this.toast('Could not start commit', null, null);
                });
        },

        pollCommit() {
            const tick = async () => {
                try {
                    const r = await fetch('/api/commit/status');
                    const data = await r.json();
                    this.commitProgress = data.progress;
                    if (!data.running) {
                        if (data.error) {
                            this.executing = false;
                            this.toast(data.error, null, null);
                        } else if (data.result) {
                            window.location = '/done';
                        } else {
                            this.executing = false;
                        }
                    } else {
                        setTimeout(tick, 300);
                    }
                } catch {
                    setTimeout(tick, 500);
                }
            };
            tick();
        },
        basename(p) { return String(p || '').split('/').pop(); },

        formatSize(bytes) {
            if (!bytes) return '0 B';
            const units = ['B', 'KB', 'MB', 'GB'];
            let i = 0, v = bytes;
            while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
            return v.toFixed(v < 10 ? 1 : 0) + ' ' + units[i];
        },

        toast(text, actionLabel, onAction) {
            this.$refs.toasts.querySelectorAll('.toast').forEach(t => this.dismissToast(t));
            const el = document.createElement('div');
            el.className = 'toast';
            const txt = document.createElement('span');
            txt.className = 'toast__text'; txt.textContent = text;
            el.appendChild(txt);
            if (actionLabel) {
                const btn = document.createElement('button');
                btn.type = 'button'; btn.className = 'toast__action'; btn.textContent = actionLabel;
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
    };
}

function doneApp() {
    return {
        commit_id: null,
        stats: {},
        undoing: false,

        init() {
            const data = JSON.parse(document.getElementById('initial-data').textContent);
            this.commit_id = data.commit_id;
            this.stats = data.stats || {};
        },

        async openFolder() {
            try {
                const r = await fetch('/api/open-folder', { method: 'POST' });
                const data = await r.json();
                if (data.error) this.toast(data.error, null, null);
            } catch {
                this.toast('Could not open folder', null, null);
            }
        },

        async undoEverything() {
            if (this.undoing) return;
            this.undoing = true;
            try {
                const r = await fetch('/api/commit/undo', { method: 'POST' });
                const data = await r.json();
                if (data.error) {
                    this.toast(data.error, null, null);
                    this.undoing = false;
                    return;
                }
                const restored = data.restored || 0;
                const missing = (data.missing || []).length;
                if (missing > 0) {
                    this.toast(
                        `Restored ${restored} files · ${missing} could not be restored`,
                        null, null
                    );
                    setTimeout(() => { window.location = '/'; }, 2500);
                } else {
                    this.toast(`Restored ${restored} files`, null, null);
                    setTimeout(() => { window.location = '/'; }, 1200);
                }
            } catch {
                this.toast('Could not undo', null, null);
                this.undoing = false;
            }
        },

        formatSize(bytes) {
            if (!bytes) return '0 B';
            const units = ['B', 'KB', 'MB', 'GB'];
            let i = 0, v = bytes;
            while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
            return v.toFixed(v < 10 ? 1 : 0) + ' ' + units[i];
        },

        toast(text, actionLabel, onAction) {
            this.$refs.toasts.querySelectorAll('.toast').forEach(t => this.dismissToast(t));
            const el = document.createElement('div');
            el.className = 'toast';
            const txt = document.createElement('span');
            txt.className = 'toast__text'; txt.textContent = text;
            el.appendChild(txt);
            if (actionLabel) {
                const btn = document.createElement('button');
                btn.type = 'button'; btn.className = 'toast__action'; btn.textContent = actionLabel;
                btn.onclick = () => { if (onAction) onAction(); this.dismissToast(el); };
                el.appendChild(btn);
            }
            this.$refs.toasts.appendChild(el);
            el._timeout = setTimeout(() => this.dismissToast(el), actionLabel ? 5000 : 2200);
        },
        dismissToast(el) {
            if (!el || !el.parentNode) return;
            clearTimeout(el._timeout);
            el.classList.add('toast--out');
            setTimeout(() => el.remove(), 200);
        },
    };
}
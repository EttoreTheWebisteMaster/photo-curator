function curatorApp() {
    return {
        groups: [],
        selectedId: null,
        selectedDetail: null,
        loadingDetail: false,
        saving: false,
        showShortcuts: false,
        shortcutsReturnFocus: null,
        showCompletionDialog: false,
        completionReturnFocus: null,
        showHintBar: false,
        undoStack: [],
        sidebarWidth: 248,
        detailWidth: 304,
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
            this.suggestedCount = data.suggestedCount || 0;
            this.$watch('pendingCount', (count, previous) => {
                if (previous > 0 && count === 0 && !this.saving) this.openCompletionDialog();
            });
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
        get comparisonPhotos() {
            return this.selectedDetail?.photos || [];
        },
        get pendingCount() {
            return this.groups.filter(g => g.status === 'pending').length;
        },
        get reviewFinished() {
            return this.groups.length > 0 && this.pendingCount === 0;
        },
        get completionNextStep() {
            if (this.rejectedCount > 0) {
                return {
                    href: '/rejected',
                    title: 'Review rejected photos',
                    detail: `${this.rejectedCount} ${this.rejectedCount === 1 ? 'photo' : 'photos'} still need a final review.`,
                };
            }
            if (this.suggestedCount > 0) {
                return {
                    href: '/suggested',
                    title: 'Review suggested photos',
                    detail: `${this.suggestedCount} ${this.suggestedCount === 1 ? 'photo' : 'photos'} still need a final review.`,
                };
            }
            return {
                href: '/confirm',
                title: 'Review the organization plan',
                detail: 'Check the file moves before anything is organized.',
            };
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
            const winner = group.photos?.find(photo => photo.id === group.current_winner);
            return group.thumb || winner?.thumb || group.photos?.[0]?.thumb || '';
        },
        photoLabel(photo) {
            return `Photo ${photo.id}`;
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
                if (el && el.scrollIntoView) {
                    const behavior = window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
                    el.scrollIntoView({ block: 'nearest', behavior });
                }
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
            if (!d || d.status === 'deleted' || this.saving || d.current_winner === photoId) return false;
            const prevWinner = d.current_winner;
            const prevStatus = d.status;
            const g = this.groups.find(g => g.id === d.id);
            const prevGroupStatus = g?.status;
            d.current_winner = photoId;
            if (d.status === 'completed') {
                d.status = 'pending';
                if (g) g.status = 'pending';
            }
            if (this.isFixtures) {
                this._fixturesMark(photoId);
                return true;
            }
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
                if (g) g.status = prevGroupStatus;
                this.toast('Could not save choice', null, null);
                return false;
            } finally {
                this.saving = false;
            }
            return true;
        },

        async selectFromLightbox(photoId) {
            const d = this.selectedDetail;
            const photo = d?.photos.find(candidate => candidate.id === photoId);
            if (!d || !photo || d.status === 'deleted') return false;

            const saved = await this.promotePhoto(photoId);
            if (!saved) return false;

            this.toast(`${this.photoLabel(photo)} selected`, null, null);
            const index = this.groups.findIndex(group => group.id === d.id);
            const orderedGroups = [
                ...this.groups.slice(index + 1),
                ...this.groups.slice(0, index),
            ];
            const next = orderedGroups.find(group => group.status === 'pending')
                || orderedGroups.find(group => group.status !== 'deleted');
            if (next) {
                this.select(next.id).then(() => {
                    this.$nextTick(() => this.$refs['group-' + next.id]?.focus());
                });
            }
            return true;
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
            else if (this.groups.length > 0) this.openCompletionDialog();
        },

        openCompletionDialog() {
            if (this.showCompletionDialog) return;
            this.completionReturnFocus = document.activeElement;
            this.showCompletionDialog = true;
            this.$nextTick(() => this.$refs.completionClose?.focus());
        },
        closeCompletionDialog() {
            this.showCompletionDialog = false;
            const returnFocus = this.completionReturnFocus;
            this.$nextTick(() => {
                if (returnFocus?.isConnected && returnFocus.tabIndex >= 0 && !returnFocus.disabled) {
                    returnFocus.focus();
                } else {
                    document.querySelector('.group-row[aria-current="true"]')?.focus();
                }
            });
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
            if (!d || !d.photos || !photoId) return;
            const photos = d.photos.map(p => ({
                id: p.id,
                url: p.url,
                label: this.photoLabel(p),
            }));
            const idx = photos.findIndex(p => p.id === photoId);
            if (idx === -1) return;
            this.$dispatch('open-lightbox', {
                photos,
                index: idx,
                onSelect: this.selectFromLightbox.bind(this),
            });
        },

        /* ---------- Keyboard ---------- */
        onKey(e) {
            const tag = (e.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
            if (e.target.closest?.('.lightbox')) return;
            const nativeControl = e.target.closest?.('button, a, select, [contenteditable="true"]');
            if (nativeControl && e.key !== '?' && e.key !== 'Escape') return;
            if (e.key === 'Escape') {
                if (this.showShortcuts) { this.closeShortcuts(); e.preventDefault(); return; }
                this.selectedId = null;
                e.preventDefault();
                return;
            }
            if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z')) { this.undo(); e.preventDefault(); return; }
            if (e.metaKey || e.ctrlKey || e.altKey) return;
            if (e.key === '?') {
                if (this.showShortcuts) this.closeShortcuts();
                else this.openShortcuts();
                e.preventDefault();
                return;
            }
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

        openShortcuts() {
            this.shortcutsReturnFocus = document.activeElement;
            this.showShortcuts = true;
            this.$nextTick(() => this.$refs.shortcutsClose?.focus());
        },
        closeShortcuts() {
            this.showShortcuts = false;
            const returnFocus = this.shortcutsReturnFocus;
            this.$nextTick(() => {
                if (returnFocus?.isConnected) returnFocus.focus();
            });
        },
        trapDialogFocus(e) {
            const controls = [...e.currentTarget.querySelectorAll(
                'button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])'
            )];
            if (controls.length === 0) return;
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
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
            if (which === 'sidebar') this.sidebarWidth = 248;
            else                     this.detailWidth  = 304;
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
        returnFocus: null, onSelect: null,
        show(photos, startIndex = 0) {
            this.photos = photos; this.index = startIndex;
            this.open = true; this.zoom = false;
            this.returnFocus = document.activeElement;
            document.body.style.overflow = 'hidden';
            this.$nextTick(() => this.$refs.dialog?.focus());
        },
        close(restoreFocus = true) {
            this.open = false;
            this.zoom = false;
            document.body.style.overflow = '';
            const returnFocus = this.returnFocus;
            if (restoreFocus) {
                this.$nextTick(() => {
                    if (returnFocus?.isConnected) returnFocus.focus();
                });
            }
        },
        trapFocus(e) {
            const controls = [...e.currentTarget.querySelectorAll(
                'button:not(:disabled), a[href], [tabindex]:not([tabindex="-1"])'
            )].filter(control => control.getClientRects().length > 0);
            if (controls.length === 0) return;
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (document.activeElement === e.currentTarget) {
                e.preventDefault();
                (e.shiftKey ? last : first).focus();
            } else if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        },
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
        selectCurrent() {
            return this.onSelect?.(this.current?.id) ?? Promise.resolve(false);
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
        picking: false,
        submitting: false,
        error: '',

        folderLabel() {
            const parts = String(this.folder || '').replace(/[\\/]+$/g, '').split(/[\\/]/);
            return parts[parts.length - 1] || this.folder;
        },

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
        clockTimer: null,
        clock: Date.now(),

        start() {
            this.poll();
            this.timer = setInterval(() => this.poll(), 500);
            this.clockTimer = setInterval(() => { this.clock = Date.now(); }, 250);
        },

        async poll() {
            try {
                const r = await fetch('/api/analyze/status');
                if (!r.ok) throw new Error(`Status check failed (${r.status})`);
                this.state = await r.json();
            } catch (error) {
                this.state.error = 'Connection interrupted. Trying to reconnect…';
            }
            if (!this.state.running && this.state.finished_at) {
                clearInterval(this.timer);
                this.timer = null;
                clearInterval(this.clockTimer);
                this.clockTimer = null;
                if (this.state.final && this.state.final.mode !== 'cancelled') {
                    // Auto-advance after a moment so the user sees the ✓ marks.
                    setTimeout(() => { window.location.href = '/'; }, 1200);
                }
            }
        },

        async cancel() {
            try {
                const r = await fetch('/api/analyze/cancel', { method: 'POST' });
                if (!r.ok) throw new Error(`Cancel failed (${r.status})`);
            } catch {
                this.state.error = 'Could not cancel the analysis. Please try again.';
            }
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

        taskProgress(task) {
            if (task.status === 'done') return 100;
            if (!task.total) return 0;
            return Math.max(0, Math.min(100, Math.round((task.count / task.total) * 100)));
        },

        taskProgressText(task) {
            if (task.status === 'done') return task.summary || 'Step complete';
            if (task.status === 'error') return task.summary || 'This step could not be completed';
            if (task.total > 0) {
                const unit = task.id === 'group' ? 'comparisons' : 'photos';
                return `${task.count.toLocaleString()} of ${task.total.toLocaleString()} ${unit}`;
            }
            if (task.status === 'running') return 'Preparing this step…';
            return 'Waiting for the previous step';
        },

        taskTimeLabel(task) {
            if (task.status === 'pending') return '';
            const start = task.started_at || (task.id === 'scan' ? this.state.started_at : null);
            if (!start) return '';
            const end = task.finished_at || (task.status === 'running' ? this.clock / 1000 : start);
            const duration = Math.max(0, end - start);
            const elapsed = task.status === 'done' ? Math.ceil(duration) : Math.floor(duration);
            if (task.status === 'done') return `Finished in ${this.formatDuration(elapsed)}`;
            if (task.status === 'error') return `Stopped after ${this.formatDuration(elapsed)}`;
            if (task.status === 'running') {
                const remaining = task.eta_seconds === 0
                    ? ' · finishing this step'
                    : Number.isFinite(task.eta_seconds) && task.eta_seconds > 0
                        ? ` · about ${this.formatDuration(Math.ceil(task.eta_seconds))} left`
                        : ' · estimating remaining time';
                return `${this.formatDuration(elapsed)} elapsed${remaining}`;
            }
            return '';
        },

        formatDuration(seconds) {
            const wholeSeconds = Math.max(0, Math.floor(seconds));
            const minutes = Math.floor(wholeSeconds / 60);
            const remainder = wholeSeconds % 60;
            return minutes ? `${minutes}m ${String(remainder).padStart(2, '0')}s` : `${remainder}s`;
        },

        get overallTimeLabel() {
            if (!this.state.started_at) return 'Elapsed time starts when analysis begins';
            const end = this.state.finished_at || (this.state.running ? this.clock / 1000 : this.state.started_at);
            const elapsed = this.formatDuration(Math.max(0, Math.floor(end - this.state.started_at)));
            return this.state.running ? `${elapsed} elapsed` : `Total time ${elapsed}`;
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
            if (e.target.closest?.('button, a, select, [contenteditable="true"]')) return;
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
        batchName: '',
        batchNameError: '',
        previewUpdating: false,
        previewTimer: null,
        previewRequestId: 0,
        executing: false,
        commitProgress: null,

        async load() {
            this.loading = true;
            await this.refreshPlan();
            this.loading = false;
        },

        normalizedBatchName(value = this.batchName) {
            return [...String(value || '')
                .normalize('NFKC')
                .trim()
                .replace(/\s+/gu, '-')
                .replace(/[^\p{L}\p{N}_-]/gu, '')
                .replace(/-+/g, '-')]
                .slice(0, 48)
                .join('')
                .replace(/^[-_]+|[-_]+$/g, '')
                .replace(/[-_]+$/g, '');
        },

        updateBatchName(value) {
            this.batchName = value;
            this.previewRequestId++;
            clearTimeout(this.previewTimer);

            if (value.trim() && !this.normalizedBatchName()) {
                this.batchNameError = 'Use letters, numbers, spaces, hyphens, or underscores.';
                this.previewUpdating = false;
                return;
            }

            this.batchNameError = '';
            this.previewUpdating = true;
            this.previewTimer = setTimeout(() => this.refreshPlan(), 250);
        },

        exampleFileName() {
            const name = this.normalizedBatchName();
            return `YYYYMMDD_${name ? `${name}_` : ''}001.jpg`;
        },

        async refreshPlan() {
            const requestId = ++this.previewRequestId;
            this.previewUpdating = true;
            try {
                const r = await fetch('/api/commit/preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ batch_name: this.normalizedBatchName() }),
                });
                const body = await r.json();
                if (requestId !== this.previewRequestId) return;
                if (!r.ok) {
                    this.data = { stats: null, errors: [body.error || 'Preview failed'], sample: [] };
                } else {
                    this.data = body;
                }
            } catch {
                if (requestId === this.previewRequestId) {
                    this.data = { stats: null, errors: ['Could not refresh the file plan. Try again.'], sample: [] };
                }
            } finally {
                if (requestId === this.previewRequestId) this.previewUpdating = false;
            }
        },

        percentage(count) {
            const total = this.data.stats?.total_moves || 0;
            return `${total ? Math.round((count / total) * 100) : 0}%`;
        },

        execute() {
            if (this.executing || this.loading || this.previewUpdating || this.batchNameError) return;
            if (this.data.errors?.length) return;
            if (!this.data.stats || !this.data.stats.total_moves) return;
            this.executing = true;
            this.commitProgress = null;
            fetch('/api/commit/execute', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ batch_name: this.normalizedBatchName() }),
            })
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
        basename(p) { return String(p || '').replace(/\\/g, '/').split('/').pop(); },
        destinationFolder(role) {
            const root = this.basename(this.data.stats?.curator_root);
            return `${root}/${role === 'winner' ? 'winner' : 'rejected'}`;
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

        totalCount() {
            return ['winner', 'rejected'].reduce(
                (total, role) => total + (Number(this.stats[role]?.count) || 0),
                0
            );
        },

        totalBytes() {
            return ['winner', 'rejected'].reduce(
                (total, role) => total + (Number(this.stats[role]?.bytes) || 0),
                0
            );
        },

        organizedSummary() {
            const count = this.totalCount();
            return `${this.formatCount(count)} ${count === 1 ? 'file is' : 'files are'} now organized.`;
        },

        formatCount(count) {
            return new Intl.NumberFormat().format(Number(count) || 0);
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

function suggestedApp() {
    return {
        photos: [],
        undoStack: [],

        async load() { await this.refresh(); },

        async refresh() {
            try {
                const r = await fetch('/api/suggested');
                const data = await r.json();
                this.photos = data.photos || [];
            } catch {}
        },

        async keep(photoId) {
            const idx = this.photos.findIndex(p => p.id === photoId);
            if (idx === -1) return;
            const [photo] = this.photos.splice(idx, 1);
            try {
                await fetch(`/api/suggested/${photoId}/keep`, { method: 'POST' });
                this.pushUndo(() => { this.photos.splice(idx, 0, photo); });
            } catch {
                this.photos.splice(idx, 0, photo);
                this.toast('Could not save', null, null);
            }
        },

        async reject(photoId) {
            const idx = this.photos.findIndex(p => p.id === photoId);
            if (idx === -1) return;
            const [photo] = this.photos.splice(idx, 1);
            try {
                await fetch(`/api/suggested/${photoId}/reject`, { method: 'POST' });
                this.pushUndo(() => { this.photos.splice(idx, 0, photo); });
            } catch {
                this.photos.splice(idx, 0, photo);
                this.toast('Could not save', null, null);
            }
        },

        async acceptAll() {
            if (this.photos.length === 0) return;
            const snapshot = this.photos.slice();
            this.photos = [];
            try {
                const r = await fetch('/api/suggested/accept-all', { method: 'POST' });
                const data = await r.json();
                this.toast(`Rejected ${data.count} photos`, 'Undo', () => this.refresh());
            } catch {
                this.photos = snapshot;
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

        onKey(e) {
            const tag = (e.target.tagName || '').toLowerCase();
            if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
            if (e.target.closest?.('button, a, select, [contenteditable="true"]')) return;
            if ((e.metaKey || e.ctrlKey) && (e.key === 'z' || e.key === 'Z')) {
                const fn = this.undoStack.pop();
                if (fn) { fn(); this.toast('Undone', null, null); }
                e.preventDefault(); return;
            }
            if (e.metaKey || e.ctrlKey || e.altKey) return;
            if (this.photos.length === 0) return;
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

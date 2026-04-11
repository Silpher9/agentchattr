// channels.js -- Channel tabs, switching, filtering, CRUD
// Extracted from chat.js PR 4.  Reads shared state via window.* bridges.

'use strict';

// ---------------------------------------------------------------------------
// State (local to channels)
// ---------------------------------------------------------------------------

const _channelScrollMsg = {};  // channel name -> message ID at top of viewport

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function _getTopVisibleMsgId() {
    const scroll = document.getElementById('timeline');
    const container = document.getElementById('messages');
    if (!scroll || !container) return null;
    const rect = scroll.getBoundingClientRect();
    for (const el of container.children) {
        if (el.style.display === 'none' || !el.dataset.id) continue;
        const elRect = el.getBoundingClientRect();
        if (elRect.bottom > rect.top) return el.dataset.id;
    }
    return null;
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderChannelTabs() {
    const container = document.getElementById('channel-tabs');
    if (!container) return;

    // Preserve inline create input if it exists
    const existingCreate = container.querySelector('.channel-inline-create');
    container.innerHTML = '';

    for (const name of window.channelList) {
        const tab = document.createElement('button');
        tab.className = 'channel-tab' + (name === window.activeChannel ? ' active' : '');
        tab.dataset.channel = name;

        const label = document.createElement('span');
        label.className = 'channel-tab-label';
        label.textContent = '# ' + name;
        tab.appendChild(label);

        const unread = window.channelUnread[name] || 0;
        if (unread > 0 && name !== window.activeChannel) {
            const dot = document.createElement('span');
            dot.className = 'channel-unread-dot';
            dot.textContent = unread > 99 ? '99+' : unread;
            tab.appendChild(dot);
        }

        // Issue #13: edit + archive icons for non-general tabs (visible
        // on hover via CSS). The destructive trash icon is no longer on
        // active tabs — it only appears inside the archived-channels
        // popover as "Delete permanently" for channels that have
        // already been archived.
        if (name !== 'general') {
            const actions = document.createElement('span');
            actions.className = 'channel-tab-actions';

            const editBtn = document.createElement('button');
            editBtn.className = 'ch-edit-btn';
            editBtn.title = 'Rename';
            editBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M11.5 2.5l2 2L5 13H3v-2L11.5 2.5z" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/></svg>';
            editBtn.onclick = (e) => { e.stopPropagation(); showChannelRenameDialog(name); };
            actions.appendChild(editBtn);

            const archBtn = document.createElement('button');
            archBtn.className = 'ch-archive-btn';
            archBtn.title = 'Archive (hide from tabs, history kept)';
            // Box/archive icon
            archBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M2 4h12v3H2V4zM3 7h10v6H3V7zM6.5 9.5h3" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>';
            archBtn.onclick = (e) => { e.stopPropagation(); archiveChannel(name); };
            actions.appendChild(archBtn);

            tab.appendChild(actions);
        }

        tab.onclick = (e) => {
            if (e.target.closest('.channel-tab-actions')) return;
            if (name === window.activeChannel) {
                // Second click on active tab -- toggle edit controls
                tab.classList.toggle('editing');
            } else {
                // Clear any editing state, switch channel
                document.querySelectorAll('.channel-tab.editing').forEach(t => t.classList.remove('editing'));
                switchChannel(name);
            }
        };

        container.appendChild(tab);
    }

    // Re-append inline create if it was open
    if (existingCreate) {
        container.appendChild(existingCreate);
    }

    // Update add button disabled state
    const addBtn = document.getElementById('channel-add-btn');
    if (addBtn) {
        addBtn.classList.toggle('disabled', window.channelList.length >= 8);
    }
}

// ---------------------------------------------------------------------------
// Switch / filter
// ---------------------------------------------------------------------------

function switchChannel(name) {
    if (name === window.activeChannel) return;
    // Save top-visible message ID for current channel
    const topId = _getTopVisibleMsgId();
    if (topId) _channelScrollMsg[window.activeChannel] = topId;
    window._setActiveChannel(name);
    window.channelUnread[name] = 0;
    localStorage.setItem('agentchattr-channel', name);
    filterMessagesByChannel();
    renderChannelTabs();
    renderRulesPanel();
    Store.set('activeChannel', name);
    // Restore: scroll to saved message, or bottom if none saved
    const savedId = _channelScrollMsg[name];
    if (savedId) {
        const el = document.querySelector(`.message[data-id="${savedId}"]`);
        if (el) { el.scrollIntoView({ block: 'start' }); return; }
    }
    window.scrollToBottom();
}

function filterMessagesByChannel() {
    const container = document.getElementById('messages');
    if (!container) return;

    for (const el of container.children) {
        const ch = el.dataset.channel || 'general';
        el.style.display = ch === window.activeChannel ? '' : 'none';
    }
}

// ---------------------------------------------------------------------------
// Create
// ---------------------------------------------------------------------------

function showChannelCreateDialog() {
    if (window.channelList.length >= 8) return;
    const tabs = document.getElementById('channel-tabs');
    // Remove existing inline create if any
    tabs.querySelector('.channel-inline-create')?.remove();

    // Hide the + button while creating
    const addBtn = document.getElementById('channel-add-btn');
    if (addBtn) addBtn.style.display = 'none';

    const wrapper = document.createElement('div');
    wrapper.className = 'channel-inline-create';

    const prefix = document.createElement('span');
    prefix.className = 'channel-input-prefix';
    prefix.textContent = '#';
    wrapper.appendChild(prefix);

    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 20;
    input.placeholder = 'channel-name';
    wrapper.appendChild(input);

    const cleanup = () => { wrapper.remove(); if (addBtn) addBtn.style.display = ''; };

    const confirm = document.createElement('button');
    confirm.className = 'confirm-btn';
    confirm.innerHTML = '&#10003;';
    confirm.title = 'Create';
    confirm.onclick = () => { _submitInlineCreate(input, wrapper); if (addBtn) addBtn.style.display = ''; };
    wrapper.appendChild(confirm);

    const cancel = document.createElement('button');
    cancel.className = 'cancel-btn';
    cancel.innerHTML = '&#10005;';
    cancel.title = 'Cancel';
    cancel.onclick = cleanup;
    wrapper.appendChild(cancel);

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); _submitInlineCreate(input, wrapper); if (addBtn) addBtn.style.display = ''; }
        if (e.key === 'Escape') cleanup();
    });
    input.addEventListener('input', () => {
        input.value = input.value.toLowerCase().replace(/[^a-z0-9\-]/g, '');
    });

    tabs.appendChild(wrapper);
    input.focus();
}

function _submitInlineCreate(input, wrapper) {
    const name = input.value.trim().toLowerCase();
    if (!name || !/^[a-z0-9][a-z0-9\-]{0,19}$/.test(name)) return;
    if (window.channelList.includes(name)) { input.focus(); return; }
    window._setPendingChannelSwitch(name);
    window.ws.send(JSON.stringify({ type: 'channel_create', name }));
    wrapper.remove();
}

// ---------------------------------------------------------------------------
// Rename
// ---------------------------------------------------------------------------

function showChannelRenameDialog(oldName) {
    const tabs = document.getElementById('channel-tabs');
    tabs.querySelector('.channel-inline-create')?.remove();

    // Find the tab being renamed so we can insert the input in its place
    const targetTab = tabs.querySelector(`.channel-tab[data-channel="${oldName}"]`);

    const wrapper = document.createElement('div');
    wrapper.className = 'channel-inline-create';

    const prefix = document.createElement('span');
    prefix.className = 'channel-input-prefix';
    prefix.textContent = '#';
    wrapper.appendChild(prefix);

    const input = document.createElement('input');
    input.type = 'text';
    input.maxLength = 20;
    input.value = oldName;
    wrapper.appendChild(input);

    const cleanup = () => {
        wrapper.remove();
        if (targetTab) targetTab.style.display = '';
    };

    const confirm = document.createElement('button');
    confirm.className = 'confirm-btn';
    confirm.innerHTML = '&#10003;';
    confirm.title = 'Rename';
    confirm.onclick = () => {
        const newName = input.value.trim().toLowerCase();
        if (!newName || !/^[a-z0-9][a-z0-9\-]{0,19}$/.test(newName)) return;
        if (newName !== oldName) {
            window.ws.send(JSON.stringify({ type: 'channel_rename', old_name: oldName, new_name: newName }));
            if (window.activeChannel === oldName) {
                window._setActiveChannel(newName);
                localStorage.setItem('agentchattr-channel', newName);
                Store.set('activeChannel', newName);
            }
        }
        cleanup();
    };
    wrapper.appendChild(confirm);

    const cancel = document.createElement('button');
    cancel.className = 'cancel-btn';
    cancel.innerHTML = '&#10005;';
    cancel.title = 'Cancel';
    cancel.onclick = cleanup;
    wrapper.appendChild(cancel);

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); confirm.click(); }
        if (e.key === 'Escape') cleanup();
    });
    input.addEventListener('input', () => {
        input.value = input.value.toLowerCase().replace(/[^a-z0-9\-]/g, '');
    });

    // Insert inline next to the tab, hide the original tab
    if (targetTab) {
        targetTab.style.display = 'none';
        targetTab.insertAdjacentElement('afterend', wrapper);
    } else {
        tabs.appendChild(wrapper);
    }
    input.select();
}

// ---------------------------------------------------------------------------
// Delete
// ---------------------------------------------------------------------------

function deleteChannel(name) {
    if (name === 'general') return;
    const tab = document.querySelector(`.channel-tab[data-channel="${name}"]`);
    if (!tab || tab.classList.contains('confirm-delete')) return;

    const label = tab.querySelector('.channel-tab-label');
    const actions = tab.querySelector('.channel-tab-actions');
    const originalText = label.textContent;
    const originalOnclick = tab.onclick;

    tab.classList.add('confirm-delete');
    tab.classList.remove('editing');
    label.textContent = `delete #${name}?`;
    if (actions) actions.style.display = 'none';

    const confirmBar = document.createElement('span');
    confirmBar.className = 'channel-delete-confirm';

    const tickBtn = document.createElement('button');
    tickBtn.className = 'ch-confirm-yes';
    tickBtn.title = 'Confirm delete';
    tickBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M3 8.5l3.5 3.5 6.5-7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    const crossBtn = document.createElement('button');
    crossBtn.className = 'ch-confirm-no';
    crossBtn.title = 'Cancel';
    crossBtn.innerHTML = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';

    confirmBar.appendChild(tickBtn);
    confirmBar.appendChild(crossBtn);
    tab.appendChild(confirmBar);

    const revert = () => {
        tab.classList.remove('confirm-delete');
        label.textContent = originalText;
        if (actions) actions.style.display = '';
        confirmBar.remove();
        tab.onclick = originalOnclick;
        document.removeEventListener('click', outsideClick);
    };

    tickBtn.onclick = (e) => {
        e.stopPropagation();
        revert();
        window.ws.send(JSON.stringify({ type: 'channel_delete', name }));
        if (window.activeChannel === name) switchChannel('general');
    };

    crossBtn.onclick = (e) => {
        e.stopPropagation();
        revert();
    };

    tab.onclick = (e) => { e.stopPropagation(); };

    const outsideClick = (e) => {
        if (!tab.contains(e.target)) revert();
    };
    setTimeout(() => document.addEventListener('click', outsideClick), 0);
}

// ---------------------------------------------------------------------------
// Issue #13: Archive / Unarchive / Delete permanently
// ---------------------------------------------------------------------------
//
// Archive is reversible (one-click, no confirm needed — user can
// unarchive from the archived-list popover any time).
// Unarchive is also one-click; the server enforces the collision and
// cap guards and sends back `channel_unarchive_error` on failure.
// Delete permanently is the only destructive path and only reachable
// from the archived-list with an inline 2-step confirm.

function archiveChannel(name) {
    if (name === 'general') return;
    if (!window.ws) return;
    window.ws.send(JSON.stringify({ type: 'channel_archive', name }));
    // If the user was looking at the channel they just archived,
    // bounce them to general so the timeline is never stuck on a
    // read-only channel after the settings broadcast lands.
    if (window.activeChannel === name) switchChannel('general');
}

function unarchiveChannel(name) {
    if (!window.ws) return;
    window.ws.send(JSON.stringify({ type: 'channel_unarchive', name }));
}

function deleteArchivedChannel(name) {
    if (!window.ws) return;
    window.ws.send(JSON.stringify({ type: 'channel_delete', name }));
}

function renderArchivedList() {
    // Update the trigger button visibility + label.
    const btn = document.getElementById('channel-archived-btn');
    const list = Array.isArray(window.archivedChannelList)
        ? window.archivedChannelList
        : [];
    if (btn) {
        if (list.length === 0) {
            btn.classList.add('hidden');
        } else {
            btn.classList.remove('hidden');
            btn.title = `${list.length} archived channel${list.length === 1 ? '' : 's'}`;
        }
    }

    // If the popover is open, re-render its body.
    const popover = document.getElementById('channel-archived-popover');
    if (!popover || popover.classList.contains('hidden')) return;

    const body = popover.querySelector('.archived-popover-body');
    if (!body) return;
    body.innerHTML = '';

    if (list.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'archived-empty';
        empty.textContent = 'No archived channels.';
        body.appendChild(empty);
        return;
    }

    for (const entry of list) {
        const name = typeof entry === 'string' ? entry : entry.name;
        const archivedBy = typeof entry === 'object' ? (entry.archived_by || '') : '';
        const archivedAt = typeof entry === 'object' ? entry.archived_at : null;

        const row = document.createElement('div');
        row.className = 'archived-row';
        row.dataset.channel = name;

        const label = document.createElement('div');
        label.className = 'archived-row-label';
        const nameEl = document.createElement('span');
        nameEl.className = 'archived-row-name';
        nameEl.textContent = '# ' + name;
        label.appendChild(nameEl);
        if (archivedAt || archivedBy) {
            const meta = document.createElement('span');
            meta.className = 'archived-row-meta';
            const parts = [];
            if (archivedAt) {
                try {
                    const d = new Date(archivedAt * 1000);
                    parts.push(d.toLocaleDateString());
                } catch (_) { /* ignore */ }
            }
            if (archivedBy) parts.push('by ' + archivedBy);
            meta.textContent = parts.join(' · ');
            label.appendChild(meta);
        }
        row.appendChild(label);

        const actions = document.createElement('div');
        actions.className = 'archived-row-actions';

        const unarchBtn = document.createElement('button');
        unarchBtn.className = 'archived-action archived-action-unarchive';
        unarchBtn.textContent = 'Unarchive';
        unarchBtn.onclick = (e) => { e.stopPropagation(); unarchiveChannel(name); };
        actions.appendChild(unarchBtn);

        const delBtn = document.createElement('button');
        delBtn.className = 'archived-action archived-action-delete';
        delBtn.textContent = 'Delete permanently';
        delBtn.onclick = (e) => {
            e.stopPropagation();
            // Inline 2-step confirm for the destructive path.
            if (row.classList.contains('confirm-delete')) return;
            row.classList.add('confirm-delete');
            const originalText = delBtn.textContent;
            delBtn.textContent = 'Confirm delete?';
            unarchBtn.style.display = 'none';

            const cancelBtn = document.createElement('button');
            cancelBtn.className = 'archived-action archived-action-cancel';
            cancelBtn.textContent = 'Cancel';
            actions.appendChild(cancelBtn);

            const revert = () => {
                row.classList.remove('confirm-delete');
                delBtn.textContent = originalText;
                unarchBtn.style.display = '';
                cancelBtn.remove();
                delBtn.onclick = (ev) => {
                    ev.stopPropagation();
                    deleteArchivedChannel(name);
                };
            };

            // Swap the onclick so the next click fires the delete.
            delBtn.onclick = (ev) => {
                ev.stopPropagation();
                deleteArchivedChannel(name);
            };
            cancelBtn.onclick = (ev) => { ev.stopPropagation(); revert(); };
        };
        actions.appendChild(delBtn);

        row.appendChild(actions);
        body.appendChild(row);
    }
}

function toggleArchivedPopover(force) {
    const popover = document.getElementById('channel-archived-popover');
    if (!popover) return;
    const willShow = force !== undefined
        ? force
        : popover.classList.contains('hidden');
    if (willShow) {
        popover.classList.remove('hidden');
        renderArchivedList();
        // Close on outside click
        setTimeout(() => {
            document.addEventListener('click', _archivedPopoverOutsideClick);
        }, 0);
    } else {
        popover.classList.add('hidden');
        document.removeEventListener('click', _archivedPopoverOutsideClick);
    }
}

function _archivedPopoverOutsideClick(e) {
    const popover = document.getElementById('channel-archived-popover');
    const btn = document.getElementById('channel-archived-btn');
    if (!popover) return;
    if (popover.contains(e.target)) return;
    if (btn && btn.contains(e.target)) return;
    toggleArchivedPopover(false);
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

function _channelsInit() {
    // Nothing to do yet -- channel rendering is driven by chat.js calling
    // renderChannelTabs() and filterMessagesByChannel() at the right times.
}

// ---------------------------------------------------------------------------
// Window exports (for inline onclick in index.html and chat.js callers)
// ---------------------------------------------------------------------------

window.showChannelCreateDialog = showChannelCreateDialog;
window.switchChannel = switchChannel;
window.filterMessagesByChannel = filterMessagesByChannel;
window.renderChannelTabs = renderChannelTabs;
window.deleteChannel = deleteChannel;
window.showChannelRenameDialog = showChannelRenameDialog;
window.archiveChannel = archiveChannel;
window.unarchiveChannel = unarchiveChannel;
window.deleteArchivedChannel = deleteArchivedChannel;
window.renderArchivedList = renderArchivedList;
window.toggleArchivedPopover = toggleArchivedPopover;
window.Channels = { init: _channelsInit };

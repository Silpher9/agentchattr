// launch-panel.js -- Agent launch panel: start/stop agents from the UI
'use strict';

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let _launchData = {};  // agent_name → {command, label, color, type, running, launchable, ui_launched, supports_attach, mode}
let _launchRoot = '';  // repo root path from server
let _runningInstances = [];  // concrete runtime instances from tmux/API wrappers
let _launchRefreshTimer = null;

// ---------------------------------------------------------------------------
// Toggle
// ---------------------------------------------------------------------------

function toggleLaunchPanel() {
    const panel = document.getElementById('launch-panel');
    panel.classList.toggle('hidden');
    document.getElementById('launch-toggle').classList.toggle('active', !panel.classList.contains('hidden'));
    if (!panel.classList.contains('hidden')) {
        fetchLaunchCommands();
    }
}

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

async function fetchLaunchCommands() {
    try {
        const resp = await fetch('/api/launch/commands', {
            headers: { 'X-Session-Token': window.SESSION_TOKEN },
        });
        const data = await resp.json();
        _launchRoot = data._root || '';
        _runningInstances = Array.isArray(data._running) ? data._running : [];
        delete data._root;
        delete data._running;
        _launchData = data;
        renderLaunchPanel();
    } catch (e) {
        console.error('Failed to fetch launch commands:', e);
    }
}

function isLaunchPanelOpen() {
    const panel = document.getElementById('launch-panel');
    return !!panel && !panel.classList.contains('hidden');
}

function scheduleLaunchRefresh(delay = 120) {
    if (!isLaunchPanelOpen()) return;
    clearTimeout(_launchRefreshTimer);
    _launchRefreshTimer = setTimeout(() => {
        _launchRefreshTimer = null;
        fetchLaunchCommands();
    }, delay);
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderLaunchPanel() {
    const list = document.getElementById('launch-list');
    if (!list) return;
    list.innerHTML = '';

    if (_runningInstances.length > 0) {
        const section = document.createElement('div');
        section.className = 'launch-section';
        section.innerHTML = '<div class="launch-section-title">Running Instances</div>';

        for (const runtime of _runningInstances) {
            const card = document.createElement('div');
            card.className = 'launch-card launch-runtime-card';
            card.dataset.instance = runtime.id;

            const tags = [];
            if (!runtime.registered) {
                tags.push('<span class="launch-runtime-tag orphan">orphan</span>');
            } else if (runtime.ui_launched) {
                tags.push('<span class="launch-runtime-tag ui">ui</span>');
            } else {
                tags.push('<span class="launch-runtime-tag external">external</span>');
            }
            if (runtime.transport === 'api') {
                tags.push('<span class="launch-runtime-tag">api</span>');
            }

            let actionBtns = '';
            if (runtime.stoppable) {
                actionBtns += `<button class="launch-stop-btn" onclick="event.stopPropagation();stopAgent('${runtime.id}')">■ Stop</button>`;
            }
            if (runtime.supports_attach) {
                actionBtns += `<button class="launch-attach-btn" onclick="event.stopPropagation();attachAgent('${runtime.id}')" title="Open terminal">⬛</button>`;
            }

            const sessionLine = runtime.session_name
                ? `<div class="launch-runtime-line"><code>${window.escapeHtml(runtime.session_name)}</code></div>`
                : '';
            const renameLine = runtime.name !== runtime.id
                ? `<div class="launch-runtime-line">current: <code>@${window.escapeHtml(runtime.name)}</code></div>`
                : '';

            card.innerHTML = `
                <div class="launch-card-header">
                    <span class="launch-dot" style="background:${runtime.color}"></span>
                    <div class="launch-label-group">
                        <span class="launch-label">${window.escapeHtml(runtime.label || runtime.name)}</span>
                        <span class="launch-runtime-handle"><code>@${window.escapeHtml(runtime.name)}</code></span>
                    </div>
                    <div class="launch-runtime-tags">${tags.join('')}</div>
                    <span style="flex:1"></span>
                    <div class="launch-actions">${actionBtns}</div>
                </div>
                <div class="launch-runtime-meta">
                    ${renameLine}
                    ${sessionLine}
                </div>
            `;
            section.appendChild(card);
        }
        list.appendChild(section);
    }

    const agents = Object.entries(_launchData);
    if (agents.length === 0) {
        const ghost = document.createElement('div');
        ghost.className = 'sb-ghost-card';
        ghost.innerHTML = '<div class="sb-ghost-title">No agents configured</div>';
        list.appendChild(ghost);
    } else {
        const section = document.createElement('div');
        section.className = 'launch-section';
        section.innerHTML = '<div class="launch-section-title">Launchable Agents</div>';

        for (const [name, info] of agents) {
            const card = document.createElement('div');
            card.className = 'launch-card';
            card.dataset.agent = name;

            const isRunning = info.running;
            const uiLaunched = info.ui_launched;
            const canLaunch = info.launchable;
            const isApi = info.type === 'api';

            const typeTag = isApi
                ? '<span class="launch-type-tag">API</span>'
                : '';

            const accountHtml = info.account
                ? `<span class="launch-account">${window.escapeHtml(info.account)}</span>`
                : '';

            // Action buttons
            let actionBtns = '';
            if (isRunning && uiLaunched) {
                actionBtns = `<button class="launch-stop-btn" onclick="event.stopPropagation();stopAgent('${name}')">■ Stop</button>`;
                if (info.supports_attach) {
                    actionBtns += `<button class="launch-attach-btn" onclick="event.stopPropagation();attachAgent('${name}')" title="Open terminal">⬛</button>`;
                }
            } else if (isRunning) {
                // Running externally — show status + stop + attach
                actionBtns = `<span class="launch-external-tag">running</span>`;
                actionBtns += `<button class="launch-stop-btn" onclick="event.stopPropagation();stopAgent('${name}')">■ Stop</button>`;
                if (info.supports_attach) {
                    actionBtns += `<button class="launch-attach-btn" onclick="event.stopPropagation();attachAgent('${name}')" title="Open terminal">⬛</button>`;
                }
            } else if (canLaunch) {
                if (isApi) {
                    actionBtns = `<button class="launch-start-btn" onclick="event.stopPropagation();launchAgent('${name}', 'background', this)">▶</button>`;
                } else {
                    actionBtns = `<button class="launch-start-btn" onclick="event.stopPropagation();launchAgent('${name}', 'visible', this)" title="Launch with terminal">▶</button>`;
                }
            } else {
                actionBtns = `<button class="launch-start-btn" disabled title="Add stopper to config to enable UI launch">▶</button>`;
            }

            card.innerHTML = `
                <div class="launch-card-header">
                    <span class="launch-dot" style="background:${info.color}"></span>
                    <div class="launch-label-group">
                        <span class="launch-label">${window.escapeHtml(info.label || name)}</span>
                        ${accountHtml}
                    </div>
                    ${typeTag}
                    <span style="flex:1"></span>
                    <div class="launch-actions">${actionBtns}</div>
                </div>
                <div class="launch-cmd-toggle" onclick="this.nextElementSibling.classList.toggle('hidden')">
                    ▸ <code class="launch-cmd-preview">${window.escapeHtml(info.command).substring(0, 50)}${info.command.length > 50 ? '…' : ''}</code>
                </div>
                <div class="launch-cmd-full hidden">
                    <code>${window.escapeHtml(info.command)}</code>
                </div>
                ${info.auto_approve_flag ? `<label class="launch-auto-approve ${isRunning ? 'disabled' : ''}" title="${window.escapeHtml(info.auto_approve_flag)}">
                    <input type="checkbox" data-agent="${name}" class="launch-approve-cb" ${isRunning ? 'disabled' : 'checked'}> Skip permissions
                </label>` : ''}
                <div class="launch-extra-args ${isRunning ? 'hidden' : ''}">
                    <input type="text" class="launch-extra-input" placeholder="Extra args (optional)" data-agent="${name}">
                </div>
            `;
            section.appendChild(card);
        }
        list.appendChild(section);
    }

    // Config file hotlinks
    if (_launchRoot) {
        const links = document.createElement('div');
        links.className = 'launch-config-links';
        links.innerHTML = `
            <a class="file-link" href="#" onclick="openPath('${_launchRoot}/config.toml'); return false;">config.toml</a>
            <a class="file-link" href="#" onclick="openPath('${_launchRoot}/config.local.toml'); return false;">config.local.toml</a>
        `;
        list.appendChild(links);
    }
}

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

async function launchAgent(name, mode, btn) {
    // Optimistic disable to prevent double-launch
    if (btn) btn.disabled = true;

    const input = document.querySelector(`.launch-extra-input[data-agent="${name}"]`);
    const extraArgs = input ? input.value.trim() : '';
    const cb = document.querySelector(`.launch-approve-cb[data-agent="${name}"]`);
    const autoApprove = cb ? cb.checked : false;

    try {
        const resp = await fetch(`/api/launch/${name}`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Session-Token': window.SESSION_TOKEN,
            },
            body: JSON.stringify({ extra_args: extraArgs, mode, auto_approve: autoApprove }),
        });
        const data = await resp.json();
        if (resp.ok) {
            await fetchLaunchCommands();
        } else {
            console.error('Launch failed:', data.error);
            if (btn) btn.disabled = false;
        }
    } catch (e) {
        console.error('Launch error:', e);
        if (btn) btn.disabled = false;
    }
}

async function stopAgent(name) {
    if (!confirm(`Stop agent "${name}"?`)) return;
    try {
        const resp = await fetch(`/api/launch/${encodeURIComponent(name)}/stop`, {
            method: 'POST',
            headers: { 'X-Session-Token': window.SESSION_TOKEN },
        });
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
            await fetchLaunchCommands();
            return;
        }
        alert(`Stop failed: ${data.error || resp.statusText}`);
    } catch (e) {
        console.error('Stop error:', e);
        alert(`Stop error: ${e.message}`);
    }
}

async function attachAgent(name) {
    try {
        const resp = await fetch(`/api/launch/${name}/attach`, {
            method: 'POST',
            headers: { 'X-Session-Token': window.SESSION_TOKEN },
        });
        const data = await resp.json();
        if (!resp.ok) {
            console.error('Attach failed:', data.error);
            if (data.command) alert(`Run manually:\n${data.command}`);
        } else if (!data.opened) {
            // No terminal emulator found — show command for manual use
            alert(`No terminal emulator found. Run manually:\n\n${data.command}`);
        }
    } catch (e) {
        console.error('Attach error:', e);
    }
}

// ---------------------------------------------------------------------------
// Status sync — update running state from WebSocket status broadcasts
// ---------------------------------------------------------------------------

function updateLaunchStatus(statusData) {
    if (!_launchData || !statusData) return;
    let changed = false;
    for (const [name, info] of Object.entries(_launchData)) {
        const wasRunning = info.running;
        const agentStatus = statusData[name];
        const nowRunning = !!(agentStatus && (agentStatus.available || agentStatus.busy));
        // If it stopped running, clear ui_launched
        if (wasRunning && !nowRunning) info.ui_launched = false;
        info.running = nowRunning;
        if (info.running !== wasRunning) changed = true;
    }
    if (changed) renderLaunchPanel();
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

window.toggleLaunchPanel = toggleLaunchPanel;
window.launchAgent = launchAgent;
window.stopAgent = stopAgent;
window.attachAgent = attachAgent;
window.updateLaunchStatus = updateLaunchStatus;
window.fetchLaunchCommands = fetchLaunchCommands;
window.handleLaunchAgentsChanged = scheduleLaunchRefresh;

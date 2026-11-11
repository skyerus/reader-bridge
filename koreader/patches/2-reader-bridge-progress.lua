-- Reader Bridge's optional KOSync revision guard. Other sync accounts are untouched.
local DataStorage = require('datastorage')
local LuaSettings = require('luasettings')
local UIManager = require('ui/uimanager')
local Math = require('optmath')
local userpatch = require('userpatch')
local directory = debug.getinfo(1, 'S').source:sub(2):match('(.*/)')
local config = LuaSettings:open(DataStorage:getSettingsDir() .. '/readerbridge-progress-config.lua')
local storage = LuaSettings:open(DataStorage:getSettingsDir() .. '/readerbridge-progress-state.lua')
local accounts = storage:readSetting('accounts', {})
local scope
local unpack = unpack or table.unpack
local function active(owner)
    return owner and owner.settings and config:readSetting('version') == 1
        and owner.settings.custom_server == config:readSetting('endpoint')
        and owner.settings.username == config:readSetting('username')
end
local function state(owner)
    local key = owner.settings.custom_server .. '\n' .. owner.settings.username
    accounts[key] = accounts[key] or {books={}, conflicts={}}
    return accounts[key]
end
local function save()
    storage:saveSetting('accounts', accounts)
    storage:flush()
end
local function acknowledge(owner, document, revision)
    if type(revision) ~= 'string' then return end
    state(owner).books[document] = revision
    save()
end
local function within(owner, mode, action, ...)
    local previous = scope
    scope = active(owner) and {owner=owner, mode=mode} or nil
    local result = {pcall(action, owner, ...)}
    scope = previous
    if not result[1] then error(result[2]) end
    return unpack(result, 2)
end
local function copy(value)
    if type(value) ~= 'table' then return value end
    local result = {}
    for k,v in pairs(value) do result[k] = copy(v) end
    return result
end
local function conflict(owner, item)
    -- Keep the unsent location for recovery; it must never be retried as a force push.
    item = copy(item)
    if item.metadata and item.metadata.reader_bridge then item.metadata.reader_bridge.force = false end
    local items = state(owner).conflicts
    local last = items[#items]
    if not last or last.document ~= item.document or last.progress ~= item.progress
        or last.base_revision ~= item.base_revision then
        item.recorded_at = os.time()
        items[#items+1] = item
        save()
    end
end
local function pull(owner)
    if owner._rb_closed then return end
    owner.pull_timestamp = 0 -- A conflict requires a fresh read, even within native debounce.
    owner:getProgress(false, false) -- Never enables Wi-Fi or adds background polling.
end

local client_patched = false
local function patch_client()
    if client_patched then return end
    local Client = require('KOSyncClient')
    local new, get, update = Client.new, Client.get_progress, Client.update_progress
    Client.new = function(self, options)
        if scope then
            options = copy(options or {})
            options.service_spec = directory .. 'readerbridge-api.json'
            options._rb_context = scope
        end
        return new(self, options)
    end
    Client.get_progress = function(self, username, password, document, callback)
        local context = self._rb_context
        if not context then return get(self, username, password, document, callback) end
        local owner = context.owner
        return get(self, username, password, document, function(ok, body)
            if body and body.reader_bridge_revision == 'missing' then
                acknowledge(owner, document, 'missing')
                return callback(true, body)
            end
            if ok and body and body.reader_bridge_revision then
                owner._rb_remote = owner._rb_remote or {}
                owner._rb_remote[document] = body
            end
            callback(ok, body)
            -- A GET alone is not consent to overwrite. Acknowledge only when the
            -- reader actually adopted the location (or already displays it).
            if ok and body and body.reader_bridge_revision and not owner._rb_closed
                and owner:getDocumentDigest() == document
                and (owner:getLastProgress() == body.progress
                    or owner:getLastPercent() == Math.roundPercent(body.percentage)) then
                acknowledge(owner, document, body.reader_bridge_revision)
            end
        end)
    end
    Client.update_progress = function(self, username, password, document, metadata,
            progress, percentage, device, device_id, callback)
        local context = self._rb_context
        if not context then return update(self, username, password, document, metadata,
            progress, percentage, device, device_id, callback) end
        local owner = context.owner
        return update(self, username, password, document, metadata, progress, percentage,
            device, device_id, function(ok, status, body)
                if ok and body then acknowledge(owner, document, body.reader_bridge_revision) end
                if status == 409 and body and body.reader_bridge_conflict then
                    conflict(owner, {document=document, metadata=metadata, progress=progress,
                        percentage=percentage, device=device, device_id=device_id,
                        base_revision=metadata and metadata.reader_bridge and metadata.reader_bridge.base_revision})
                    -- Native updateProgress retries every non-401 failure. This
                    -- conflict is retained separately, not put back on that queue.
                    callback(false, context.mode == 'drain' and 409 or 401, body)
                    if context.mode ~= 'drain' then UIManager:nextTick(function() pull(owner) end) end
                else
                    callback(ok, status, body)
                end
            end)
    end
    local ok, Queue = pcall(require, 'KOSyncQueue')
    if ok then
        local push = Queue.push
        Queue.push = function(self, item)
            if item.metadata and item.metadata.reader_bridge then
                item = copy(item)
                -- An explicit online action must not become a delayed forced overwrite.
                item.metadata.reader_bridge.force = false
            end
            return push(self, item)
        end
    end
    client_patched = true
end

userpatch.registerPatchPluginFunc('kosync', function(Plugin)
    patch_client()
    local metadata, update, get = Plugin.getMetadata, Plugin.updateProgress, Plugin.getProgress
    local sync, close, connected, drain = Plugin.syncToProgress, Plugin._onCloseDocument,
        Plugin._onNetworkConnected, Plugin.drainQueue
    Plugin.getMetadata = function(self)
        local result = metadata(self)
        if not active(self) then return result end
        result = copy(result or {})
        result.reader_bridge = {version=1,
            base_revision=state(self).books[self:getDocumentDigest()] or 'unknown',
            force=scope ~= nil and scope.owner == self and scope.mode == 'manual'}
        return result
    end
    Plugin.updateProgress = function(self, ensure_networking, interactive, on_suspend)
        return within(self, interactive and 'manual' or 'auto', update, ensure_networking, interactive, on_suspend)
    end
    Plugin.getProgress = function(self, ...)
        return within(self, 'pull', get, ...)
    end
    Plugin.syncToProgress = function(self, progress)
        local result = sync(self, progress)
        if active(self) then
            local document = self:getDocumentDigest()
            local remote = self._rb_remote and self._rb_remote[document]
            if remote and remote.progress == progress then acknowledge(self, document, remote.reader_bridge_revision) end
        end
        return result
    end
    Plugin._onCloseDocument = function(self, ...)
        self._rb_closed = true
        return close(self, ...)
    end
    Plugin.drainQueue = function(self, done)
        if not active(self) then return drain and drain(self) end
        if self._rb_draining then return end
        local ok, Queue = pcall(require, 'KOSyncQueue')
        if not ok then if done then done() end; return end
        self._rb_draining = true
        local function finish()
            self._rb_draining = false
            if done then done() end
        end
        local function next_item()
            local queued = Queue:load()
            -- The service stores a current location, not per-day reading statistics.
            -- Send the newest pending position first; older entries for that book
            -- must not supersede it or claim a conflict after it succeeds.
            local item = queued[#queued]
            if not item then return finish() end
            local send = copy(item)
            send.metadata = send.metadata or {}
            send.metadata.reader_bridge = send.metadata.reader_bridge or {version=1, base_revision='unknown'}
            send.metadata.reader_bridge.force = false
            local sent = pcall(within, self, 'drain', function(owner)
                local Client = require('KOSyncClient')
                local client = Client:new{custom_url=owner.settings.custom_server, service_spec=owner.path .. '/api.json'}
                client:update_progress(owner.settings.username, owner.settings.userkey,
                    send.document, send.metadata, send.progress, send.percentage, send.device, send.device_id,
                    function(success, status, body)
                        if not success and status ~= 409 then return finish() end
                        -- Native drainQueue removes entries after pcall, before the
                        -- asynchronous result. Remove only this acknowledged entry,
                        -- preserving any newer entry added while the request ran.
                        local current = Queue:load()
                        for i=#current,1,-1 do
                            local candidate = current[i]
                            for _, original in ipairs(queued) do
                                local a = candidate.metadata and candidate.metadata.reader_bridge
                                local b = original.metadata and original.metadata.reader_bridge
                                if original.document == item.document and candidate.document == original.document
                                    and candidate.progress == original.progress and candidate.percentage == original.percentage
                                    and candidate.device_id == original.device_id and candidate.queued_at == original.queued_at
                                    and (a and a.base_revision) == (b and b.base_revision) then
                                    table.remove(current, i); break
                                end
                            end
                        end
                        -- A position queued while our own upload was in flight
                        -- can continue that acknowledged branch. Never rebase
                        -- after a conflict, GET, or another reader's upload.
                        if success and body and body.reader_bridge_revision then
                            for _, candidate in ipairs(current) do
                                local control = candidate.metadata and candidate.metadata.reader_bridge
                                if candidate.document == item.document and candidate.device_id == item.device_id
                                    and control and control.base_revision == send.metadata.reader_bridge.base_revision
                                    and (candidate.queued_at or 0) >= (item.queued_at or 0) then
                                    control.base_revision = body.reader_bridge_revision
                                end
                            end
                        end
                        Queue:save(current)
                        UIManager:nextTick(next_item)
                    end)
            end)
            if not sent then finish() end
        end
        next_item()
    end
    Plugin._onNetworkConnected = function(self)
        if not active(self) then return connected(self) end
        UIManager:scheduleIn(0.5, function()
            if not self._rb_closed then self:drainQueue(function() pull(self) end) end
        end)
    end
end)

-- Keep the companion tied to this entry point: on-device logs showed it was
-- skipped by later startup scans even though this progress patch still loaded.
-- The companion guards duplicate discovery within the same KOReader process.
local quiet_ok, quiet_error = pcall(dofile, directory .. '2-reader-bridge-quiet-wifi.lua')
if not quiet_ok then
    require('logger').warn('Passage: could not load quiet Wi-Fi companion:', quiet_error)
end

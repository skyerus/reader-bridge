-- Kindle's Wi-Fi daemon can reconnect to saved networks without a blocking scan.
-- Keep automatic KOSync off the interactive connection path, including after a
-- failed connection cleared wifi_was_on. Explicit network/sync actions stay native.
local Device = require('device')
if not Device:isKindle() or not Device:hasWifiRestore() then return end

local NetworkMgr = require('ui/network/manager')
local userpatch = require('userpatch')
-- The progress entry point also loads this file. This flag is process-local,
-- so every restart registers the hooks again, while normal discovery is harmless.
if userpatch._passage_quiet_wifi_registered then return end

userpatch.registerPatchPluginFunc('kosync', function(Plugin)
    local get, update = Plugin.getProgress, Plugin.updateProgress
    local resume, suspend, close = Plugin._onResume, Plugin._onSuspend, Plugin._onCloseDocument
    local connected = Plugin._onNetworkConnected
    if not (get and update and resume and suspend and close) then return end

    local function automatic(owner)
        return owner.settings and owner.settings.auto_sync
            and owner.settings.username and owner.settings.userkey
            and G_reader_settings:readSetting('wifi_enable_action') == 'turn_on'
    end

    Plugin.getProgress = function(self, ensure_networking, interactive)
        if ensure_networking and not interactive and automatic(self) then
            if self._passage_sleeping or self._passage_closed then return end
            if NetworkMgr:isConnected() then
                return get(self, false, false)
            end
            -- Reuse KOReader's bounded connectivity check and NetworkConnected
            -- event, which drains queued progress before pulling the remote page.
            -- Do not restart an existing restore or an explicit connection attempt.
            if not NetworkMgr.pending_connectivity_check and not NetworkMgr.pending_connection then
                NetworkMgr:restoreWifiAsync()
                NetworkMgr:scheduleConnectivityCheck()
                self._passage_started_restore = true
            end
            return
        end
        return get(self, ensure_networking, interactive)
    end

    Plugin.updateProgress = function(self, ensure_networking, interactive, on_suspend)
        if on_suspend and not interactive and automatic(self) then
            if not NetworkMgr:isConnected() then
                -- Save locally before sleeping; attempting a new connection here
                -- can leave its scan dialog covering the page on the next wake.
                local ok, Queue = pcall(require, 'KOSyncQueue')
                if ok and Queue.push then
                    local document = self:getDocumentDigest()
                    if document then
                        Queue:push({
                            document = document,
                            metadata = self:getMetadata(),
                            progress = self:getLastProgress(),
                            percentage = self:getLastPercent(),
                            device = self.settings.kosync_hostname or Device.model,
                            device_id = self.device_id,
                        })
                    end
                    return
                end
            end
            -- Older plugins without a queue retain their native offline handling,
            -- but must not open the connection UI during an automatic suspend.
            return update(self, false, false, on_suspend)
        end
        return update(self, ensure_networking, interactive, on_suspend)
    end

    Plugin._onResume = function(self, ...)
        self._passage_sleeping = false
        return resume(self, ...)
    end

    local function cancelRestore(owner)
        if owner._passage_started_restore then
            if NetworkMgr.pending_connectivity_check and not NetworkMgr.pending_connection then
                NetworkMgr:unscheduleConnectivityCheck()
            end
            owner._passage_started_restore = nil
        end
    end

    if connected then
        Plugin._onNetworkConnected = function(self, ...)
            self._passage_started_restore = nil
            return connected(self, ...)
        end
    end

    Plugin._onSuspend = function(self, ...)
        self._passage_sleeping = true
        cancelRestore(self)
        return suspend(self, ...)
    end

    Plugin._onCloseDocument = function(self, ...)
        self._passage_closed = true
        cancelRestore(self)
        return close(self, ...)
    end
end)

userpatch._passage_quiet_wifi_registered = true
require('logger').info('Passage: quiet automatic Wi-Fi reconnect registered')

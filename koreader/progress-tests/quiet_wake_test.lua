local root = arg[0]:match('(.*/)') .. '../patches/'
local tasks, queue, scans, pulls, pushes, restores = {}, {}, 0, 0, 0, 0
local online, kindle, action = false, true, 'turn_on'
local patch
local Device = {model='Kindle', isKindle=function() return kindle end, hasWifiRestore=function() return true end}
local Network = {isConnected=function() return online end,
    restoreWifiAsync=function() restores=restores+1 end,
    scheduleConnectivityCheck=function(self) self.pending_connectivity_check=true end,
    unscheduleConnectivityCheck=function(self) self.pending_connectivity_check=false end}
G_reader_settings = {readSetting=function() return action end}
local modules = {logger={info=function() end}, device=Device, ['ui/network/manager']=Network,
    userpatch={registerPatchPluginFunc=function(_,fn) patch=fn end},
    KOSyncQueue={push=function(_,item) queue[#queue+1]=item end}}
for name,value in pairs(modules) do package.preload[name]=function() return value end end
local function native()
    return {
        getProgress=function(_,ensure,interactive)
            if ensure and not online then scans=scans+1;return end
            pulls=pulls+1
        end,
        updateProgress=function(_,ensure)
            if ensure and not online then scans=scans+1;return end
            pushes=pushes+1
        end,
        _onResume=function(self) tasks[#tasks+1]=function() self:getProgress(true,false) end end,
        _onSuspend=function(self) self:updateProgress(true,false,true) end,
        _onCloseDocument=function(self) self:updateProgress(false,false) end,
        _onNetworkConnected=function(self) self:getProgress(false,false) end,
        getDocumentDigest=function() return 'book-digest' end,
        getMetadata=function() return {reader_bridge={base_revision='known',force=false}} end,
        getLastProgress=function() return 'last-read-page' end,
        getLastPercent=function() return .4 end,
    }
end
dofile(root .. '2-reader-bridge-quiet-wifi.lua')
local Plugin=native();patch(Plugin)
local function owner()
    return setmetatable({settings={auto_sync=true,username='fixture',userkey='hash'},device_id='kindle'}, {__index=Plugin})
end
local function tick() local f=table.remove(tasks,1);assert(f);f() end
local p=owner()
-- Failed previous restore / wifi_was_on=false: native resume requests a connection.
p:_onResume();tick()
assert(scans==0 and restores==1 and Network.pending_connectivity_check)
p:getProgress(true,false)
assert(restores==1,'must reuse pending connection, not start a second attempt')
online=true;Network.pending_connectivity_check=false;p:_onNetworkConnected()
assert(pulls==1 and not p._passage_started_restore,'successful restore must still pull')
-- Offline suspend retains the current page and revision without a Wi-Fi scan.
online=false;p:_onSuspend()
assert(scans==0 and #queue==1 and queue[1].progress=='last-read-page')
assert(queue[1].metadata.reader_bridge.base_revision=='known' and queue[1].device_id=='kindle')
p:_onResume();p:_onSuspend();tick()
assert(restores==1,'delayed wake callback must not reconnect after going back to sleep')
p:_onResume();tick();assert(restores==2)
p:_onCloseDocument();assert(not Network.pending_connectivity_check)
p:getProgress(true,false);assert(restores==2,'closed book must not reconnect later')
-- An explicit sync still uses the native dialog; user Wi-Fi policy is respected.
p=owner();p:getProgress(true,true);assert(scans==1)
action='prompt';p:getProgress(true,false);assert(scans==2 and restores==2)
action='turn_on';p.settings.auto_sync=false;p:getProgress(true,false);assert(scans==3)
p=owner();Network.pending_connection=true;p:getProgress(true,false);assert(restores==2)
Network.pending_connection=false;online=true;p:_onSuspend();assert(pushes==2)
-- Pending manual connectivity checks are never cancelled by this patch.
p=owner();p._passage_started_restore=true;Network.pending_connection=true
Network.pending_connectivity_check=true;p:_onSuspend();assert(Network.pending_connectivity_check)
-- Unsupported devices retain native behaviour.
kindle=false;patch=nil;dofile(root .. '2-reader-bridge-quiet-wifi.lua');assert(patch==nil)
print('Quiet wake: reconnect, pending connection, offline suspend, lifecycle cancellation, manual actions and device scope passed')

-- The companion must load even when only the progress entry is discovered.
local root = arg[0]:match('(.*/)') .. '../patches/'
local callbacks, kindle = {}, true
local userpatch = {registerPatchPluginFunc=function(name, fn)
    assert(name == 'kosync')
    callbacks[#callbacks+1] = fn
end}
local settings = {readSetting=function(_, _, default) return default end}
local modules = {
    datastorage={getSettingsDir=function() return '/fixture/settings' end},
    luasettings={open=function() return settings end},
    ['ui/uimanager']={}, optmath={},
    device={isKindle=function() return kindle end, hasWifiRestore=function() return true end},
    ['ui/network/manager']={}, userpatch=userpatch,
    logger={info=function() end, warn=function() end},
}
for name, module in pairs(modules) do package.preload[name]=function() return module end end
local function startup()
    callbacks = {}
    userpatch._passage_quiet_wifi_registered = nil
    dofile(root .. '2-reader-bridge-progress.lua')
    assert(#callbacks == 2, 'progress startup must also register quiet Wi-Fi')
    -- Normal discovery may still visit the companion after its explicit load.
    dofile(root .. '2-reader-bridge-quiet-wifi.lua')
    dofile(root .. '2-reader-bridge-quiet-wifi.lua')
    assert(#callbacks == 2, 'companion must register once per KOReader process')
end
startup()
startup() -- A restart must register again, without a persisted once-only flag.
kindle = false
callbacks = {}
userpatch._passage_quiet_wifi_registered = nil
dofile(root .. '2-reader-bridge-progress.lua')
assert(#callbacks == 1, 'other devices must keep only the existing progress guard')
print('Quiet startup: companion loading, duplicate discovery, restart, and device scope passed')

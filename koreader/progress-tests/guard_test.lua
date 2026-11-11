-- Exercise the installed patch at its native KOSync/queue/async-client boundaries.
local root=arg[0]:match('(.*/)') .. '../patches/'
local function copy(t)
    if type(t)~='table' then return t end
    local c={};for k,v in pairs(t) do c[k]=copy(v) end;return c
end
local endpoint='http://192.168.1.20:8085'
local files={config={version=1,endpoint=endpoint,username='fixture'}}
local Storage={open=function(_,path)
    local key=path:match('config.lua$') and 'config' or 'state'
    files[key]=files[key] or {}
    return {readSetting=function(_,k,d) local v=files[key][k];if v==nil then return d end;return v end,
        saveSetting=function(_,k,v) files[key][k]=v end,flush=function() end}
end}
local callbacks={}
local UI={nextTick=function(_,fn) callbacks[#callbacks+1]=fn end,
    scheduleIn=function(_,_,fn) callbacks[#callbacks+1]=fn end}
local function tick()
    local f=table.remove(callbacks,1);assert(f,'missing callback');f()
end
local function run()
    local n=0;while #callbacks>0 do tick();n=n+1;assert(n<100,'unexpected polling loop') end
end
local queue={}
local Queue={load=function() return copy(queue) end,save=function(_,items) queue=copy(items) end,
    push=function(_,item) item=copy(item);item.queued_at=item.queued_at or 100;queue[#queue+1]=item end}
local remote, requests={},{}
local Client={}
function Client:new(o) return setmetatable(o or {},{__index=self}) end
function Client:get_progress(_,_,document,callback) callback(true,copy(remote[document])) end
function Client:update_progress(_,_,document,metadata,progress,percentage,device,device_id,callback)
    requests[#requests+1]={document=document,metadata=copy(metadata),progress=progress,percentage=percentage,
        device=device,device_id=device_id,callback=callback,spec=self.service_spec}
end
local patch
local modules={device={isKindle=function() return false end},datastorage={getSettingsDir=function() return '/fixture/settings' end},
    luasettings=Storage,['ui/uimanager']=UI,optmath={roundPercent=function(p) return p end},
    userpatch={registerPatchPluginFunc=function(_,fn) patch=fn end},KOSyncClient=Client,KOSyncQueue=Queue}
for name,module in pairs(modules) do package.preload[name]=function() return module end end
dofile(root .. '2-reader-bridge-progress.lua')
local Plugin={}
function Plugin:getDocumentDigest() return self.document end
function Plugin:getLastProgress() return self.position end
function Plugin:getLastPercent() return self.percentage end
function Plugin:getMetadata() return {title='Fixture'} end
function Plugin:syncToProgress(position) self.position=position;self.percentage=remote[self.document].percentage end
function Plugin:getProgress()
    self.pulls=(self.pulls or 0)+1
    local c=Client:new{custom_url=self.settings.custom_server,service_spec='native-api'}
    c:get_progress('fixture','key',self.document,function(ok,body)
        if ok and body and body.progress and self.acceptRemote then self:syncToProgress(body.progress) end
    end)
end
function Plugin:updateProgress(_,interactive)
    local metadata=self:getMetadata()
    local item={document=self.document,metadata=metadata,progress=self.position,percentage=self.percentage,device='Kindle',device_id='kindle'}
    local c=Client:new{custom_url=self.settings.custom_server,service_spec='native-api'}
    c:update_progress('fixture','key',self.document,metadata,self.position,self.percentage,'Kindle','kindle',function(ok,status)
        if not ok and status~=401 then Queue:push(item) end
        if ok and interactive then self.pushedMessage=true end
    end)
end
function Plugin:_onCloseDocument() self:updateProgress(false,false) end
function Plugin:_onNetworkConnected() self.nativeConnected=true end
function Plugin:drainQueue() self.nativeDrain=true end
patch(Plugin)
local function owner(document)
    return setmetatable({settings={username='fixture',custom_server=endpoint},document=document or 'a',
        device_id='kindle',position='old',percentage=.3,path='native-path'}, {__index=Plugin})
end
local function reply(status,body)
    local request=table.remove(requests,1);assert(request,'missing request')
    request.callback(status==200,status,body);return request
end
local function bookState(document)
    return files.state.accounts[endpoint .. '\nfixture'].books[document or 'a']
end
local p=owner()
remote.a={progress='old',percentage=.3,reader_bridge_revision='revision-a'}
p:getProgress();assert(bookState()=='revision-a')
p.position='offline-page';p.percentage=.35;p:updateProgress(false,false)
assert(requests[1].metadata.reader_bridge.base_revision=='revision-a')
reply(nil,nil);assert(#queue==1 and queue[1].metadata.reader_bridge.force==false)
remote.a={progress='xteink-new',percentage=.5,reader_bridge_revision='revision-b'}
p.acceptRemote=true;p:_onNetworkConnected();tick()
assert(requests[1].metadata.reader_bridge.base_revision=='revision-a','must not refresh old queue token before sending')
assert(p.pulls==1,'pull must wait for queue response')
reply(409,{reader_bridge_conflict=true});run()
assert(#queue==0 and p.position=='xteink-new' and bookState()=='revision-b')
local saved=files.state.accounts[endpoint .. '\nfixture']
assert(#saved.conflicts==1 and saved.conflicts[1].progress=='offline-page')
assert(not p.pushedMessage,'rejection must not announce a successful push')

-- Automatic backward reading after an acknowledged pull is valid; manual push
-- is an explicit override. A failed manual push must lose force before retry.
p.position='earlier';p.percentage=.1;p:updateProgress(false,false)
assert(requests[1].metadata.reader_bridge.base_revision=='revision-b' and not requests[1].metadata.reader_bridge.force)
reply(200,{reader_bridge_revision='revision-c'})
p.position='explicit';p:updateProgress(true,true)
assert(requests[1].metadata.reader_bridge.force)
reply(nil,nil);assert(#queue==1 and not queue[1].metadata.reader_bridge.force)
p:drainQueue();assert(not requests[1].metadata.reader_bridge.force)
reply(409,{reader_bridge_conflict=true});run();assert(#queue==0)

-- Merely seeing a newer remote position, then declining it, does not grant
-- permission for an old local upload to replace it.
p.acceptRemote=false
remote.a={progress='other',percentage=.7,reader_bridge_revision='revision-d'}
p:getProgress();assert(bookState()=='revision-c')
p:updateProgress(false,false);assert(requests[1].metadata.reader_bridge.base_revision=='revision-c')
reply(409,{reader_bridge_conflict=true});run();assert(#queue==0 and bookState()=='revision-c')
p.acceptRemote=true;p:getProgress();assert(bookState()=='revision-d')
p:updateProgress(true,true);reply(200,{reader_bridge_revision='revision-e'});assert(p.pushedMessage)

-- Legacy queue items are untrusted, and failed asynchronous sends stay queued.
queue={{document='b',progress='legacy',percentage=.2,device='Kindle',device_id='kindle',queued_at=1}}
p:drainQueue();assert(requests[1].metadata.reader_bridge.base_revision=='unknown')
assert(#queue==1,'native pcall success is not a server acknowledgement')
reply(503,{});assert(#queue==1)
p:drainQueue();reply(401,{});assert(#queue==1)
p:drainQueue();reply(409,{reader_bridge_conflict=true});run();assert(#queue==0)

-- Send only the latest position per book, and do not remove a new queued item
-- that appeared while the asynchronous request was running.
queue={{document='c',progress='older',percentage=.1,queued_at=1},
       {document='c',progress='latest',percentage=.2,queued_at=2}}
p:drainQueue();assert(requests[1].progress=='latest')
Queue:push({document='c',progress='newer-during-request',percentage=.3,queued_at=3})
reply(200,{reader_bridge_revision='revision-f'});assert(#queue==1 and queue[1].progress=='newer-during-request')
run();assert(requests[1].progress=='newer-during-request');reply(503,{});assert(#queue==1)
queue={}

-- Closing the book prevents a delayed conflict callback from pulling again.
local closed=owner('z');closed:_onCloseDocument()
reply(409,{reader_bridge_conflict=true});run();assert(not closed.pulls)

-- Other accounts retain native settings, metadata, API spec, and event behavior.
local other=owner();other.settings.custom_server='https://example.org'
other:updateProgress(false,false);assert(not requests[1].metadata.reader_bridge)
assert(requests[1].spec=='native-api');reply(200,{})
other:_onNetworkConnected();assert(other.nativeConnected)
other:drainQueue();assert(other.nativeDrain)
assert(#callbacks==0)
-- A newer offline position on the same branch may follow our own acknowledged
-- upload, but must retain its old token if that upload was rejected.
local qowner=owner('q')
queue={{document='q',device_id='kindle',progress='one',percentage=.1,queued_at=1,
    metadata={reader_bridge={version=1,base_revision='q-base',force=false}}}}
qowner:drainQueue()
Queue:push({document='q',device_id='kindle',progress='two',percentage=.2,queued_at=2,
    metadata={reader_bridge={version=1,base_revision='q-base',force=false}}})
reply(200,{reader_bridge_revision='q-next'})
assert(queue[1].metadata.reader_bridge.base_revision=='q-next')
run();assert(requests[1].metadata.reader_bridge.base_revision=='q-next')
reply(200,{reader_bridge_revision='q-done'});run();assert(#queue==0)
print('Progress guard tests passed: stale queue, explicit push, acknowledgement, failures, races, and account isolation')

'use strict';
const $=id=>document.getElementById(id);
const token=new URLSearchParams(location.hash.slice(1)).get('token')||'';
let state=null,page='chat',busy=false,generation=null,currentAssistant=null,lastEvent=0,editingMemory=null,toastTimer;
let historyBefore=null;
let backupPending=false;
let audioContext=null,audioSource=null,audioTimer=null,speechId=null,speechNonce=0;
let recordWanted=false,recording=null,recordStarting=null,recordNonce=0,speechPending=false,asrPending=false,stopWanted=false;
function element(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;}
function toast(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').hidden=true,4200);}
async function request(path,value,raw=false){const headers={'X-Waifu-Token':token};let body;if(value!==undefined){headers['Content-Type']=raw?'audio/wav':'application/json';body=raw?value:JSON.stringify(value);}const response=await fetch(path,{method:value===undefined?'GET':'POST',headers,body,cache:'no-store'});if(!response.ok){let message='本地服務暫時無法連線';try{message=(await response.json()).error||message;}catch{}throw new Error(message);}return response;}
async function api(path,value){return(await request(path,value)).json();}
function bottom(){const log=$('messages');return log.scrollHeight-log.scrollTop-log.clientHeight<100;}
function scrollBottom(){const log=$('messages');log.scrollTop=log.scrollHeight;}
function controls(){ for(const button of document.querySelectorAll('.continue-button'))button.disabled=busy;$('send').disabled=busy;$('stop').hidden=!(busy||audioSource||recording||recordStarting||speechPending||asrPending);$('send').textContent=busy?'回覆中':'送出';}
function message(role,text,status='complete',id=null,finishReason=''){$('welcome')?.remove();const box=element('article','message '+role);const label=element('div','message-label',role==='user'?'你':'灰姑娘');const content=element('div','message-text',text);box.append(label,content);if(role==='assistant'){const actions=element('div','message-actions');const hint=element('span','',status==='cancelled'?'已停止':status==='error'?'未完成':'');hint.setAttribute('role','status');actions.append(hint);box.append(actions);box.actions=actions;box.hint=hint;}box.content=content;box.messageId=id;box.roleName=role;if(status==='generating')box.classList.add('pending');$('messages').append(box);if(id&&role==='assistant'&&status==='complete'&&text.trim()){readButton(box,id);limitHint(box,id,finishReason);}return box;}
function readButton(box,id){if(box.readButton)return;const button=element('button','','朗讀');button.type='button';button.addEventListener('click',()=>speak(id).catch(error=>toast(error.message)));box.actions.append(button);box.readButton=button;}
function limitHint(box,id,reason){if(reason!=='length'||box.continueButton)return;box.hint.textContent='已達本次長度上限';const button=element('button','continue-button','接續回覆');button.type='button';button.addEventListener('click',()=>sendMessage('請從剛才中斷的地方接續，避免重複已經說過的內容。',id));box.actions.append(button);box.continueButton=button;}
function composerCount(){$('character-count').textContent=$('composer').value.length.toLocaleString('zh-TW')+' / 12,000';}
$('composer').addEventListener('input',composerCount);
async function history(){const rows=await api('/api/history');$('messages').replaceChildren();if(!rows.length){const welcome=element('div','empty','王子，今天想聊些什麼？');welcome.id='welcome';$('messages').append(welcome);}else for(const row of rows)message(row.role,row.text,row.status,row.id,row.finish_reason);scrollBottom();return rows;}
function setPage(next){page=next;for(const button of document.querySelectorAll('.tab')){const on=button.dataset.page===next;button.classList.toggle('active',on);if(on)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');}for(const section of document.querySelectorAll('.page'))section.hidden=section.id!=='page-'+next;if(next==='memory')memories().catch(fail);if(next==='reminders')reminders().catch(fail);if(next==='history')renderHistory().catch(fail);if(next==='settings'&&state)settingsUI(state);}
function fail(error){toast(error.message||String(error));}
function reasoningStatus(bubble,event,hasAnswer){
 if(hasAnswer||stopWanted)return;
 if(event.phase==='thinking'){
  const elapsed=Number(event.elapsed_seconds),seconds=Number.isFinite(elapsed)?Math.max(0,Math.floor(elapsed)):0;
  bubble.hint.textContent=`思考中…已等待 ${seconds} 秒`;
 }else if(event.phase==='answer')bubble.hint.textContent='正在整理回答…';
}
async function sendMessage(text,continueFrom=null){
 if(busy||!text.trim())return;
 busy=true;stopWanted=false;generation=null;stopSpeech();controls();message('user',text);
 currentAssistant=message('assistant','正在想想你的話…','generating');scrollBottom();
 const bubble=currentAssistant;let output='';
 try{
  const response=await request('/api/chat',{text,...(continueFrom?{continue_from:continueFrom}:{})});
  const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';
  while(true){
   const item=await reader.read();buffer+=decoder.decode(item.value||new Uint8Array(),{stream:!item.done});let newline;
   while((newline=buffer.indexOf('\n\n'))>=0){
    const part=buffer.slice(0,newline);buffer=buffer.slice(newline+2);if(!part.startsWith('data:'))continue;
    const event=JSON.parse(part.slice(5));
    if(event.type==='start'){generation=event.id;if(stopWanted)api('/api/cancel',{id:generation}).catch(()=>{});}
    if(event.type==='reasoning')reasoningStatus(bubble,event,!!output);
    if(event.type==='delta'&&!stopWanted){
     const follow=bottom();output+=event.text;bubble.content.textContent=output;bubble.hint.textContent='';bubble.classList.remove('pending');if(follow)scrollBottom();
    }
    if(event.type==='done'){
     bubble.classList.remove('pending');bubble.messageId=event.message_id;bubble.hint.textContent='';
     if(output.trim()){
      if(event.message_id)readButton(bubble,event.message_id);
      limitHint(bubble,event.message_id,event.finish_reason);
      if(event.metrics?.trimmed_messages&&!bubble.hint.textContent)bubble.hint.textContent='較早對話已移出本次上下文';
      if(!stopWanted&&event.message_id&&state?.settings.voice_enabled&&!state.quiet)speak(event.message_id,true).catch(fail);
     }else{
      bubble.content.textContent=stopWanted?'已停止這次回覆。':'這次未產生最終回覆，請調整思考或回覆長度後再試。';
      bubble.hint.textContent=stopWanted?'已停止':'沒有最終答覆';
     }
     if(event.memory_saved)toast('已保存到記憶，隨時可以查看或刪除。');
    }
    if(event.type==='cancelled'){bubble.classList.remove('pending');bubble.content.textContent=output||'已停止這次回覆。';bubble.hint.textContent='已停止';}
    if(event.type==='error'){bubble.classList.remove('pending');bubble.content.textContent=output||event.message;bubble.hint.textContent='未完成';toast(event.message);}
   }
   if(item.done)break;
  }
 }catch(error){bubble.classList.remove('pending');bubble.content.textContent=output||'這次回覆沒有完成。';bubble.hint.textContent=error.message;fail(error);}
 finally{busy=false;generation=null;controls();pollStatus().catch(()=>{});}
}
$('composer-form').addEventListener('submit',event=>{event.preventDefault();const value=$('composer').value;if(!value.trim()||busy)return;$('composer').value='';composerCount();sendMessage(value);});
$('composer').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('composer-form').requestSubmit();}if(event.key==='Escape'&&(busy||audioSource)){event.preventDefault();stopAll();}});
for(const button of document.querySelectorAll('[data-prompt]'))button.addEventListener('click',()=>sendMessage(button.dataset.prompt));
for(const button of document.querySelectorAll('.tab'))button.addEventListener('click',()=>setPage(button.dataset.page));
$('new-topic').addEventListener('click',async()=>{try{await stopAll();await api('/api/context/new',{});$('messages').append(element('div','separator','已開始新話題，明確記憶仍會保留。'));scrollBottom();$('composer').focus();}catch(error){fail(error);}});
$('stop').addEventListener('click',()=>stopAll().catch(fail));
async function stopAll(){stopWanted=true;asrPending=false;recordNonce++;recordWanted=false;await stopRecording(false);stopSpeech();await api('/api/cancel',generation?{id:generation}:{});controls();}
let backgroundSourcesLoaded=false;
function backgroundUI(status){const b=status.background;$('background-state').textContent=b?.enabled?'已載入：'+b.description:(b?.error||'目前使用基本人設，尚未載入角色背景。');}
$('background-details').addEventListener('toggle',async()=>{if(!$('background-details').open||backgroundSourcesLoaded)return;try{const data=await api('/api/background');const list=element('ul');const labels={official_game:'遊戲資料',official_material:'官方公告',secondary:'社群整理／轉錄',user_adaptation:'專案改編'};for(const source of data.sources){const item=element('li');if(/^https?:\/\//.test(source.locator)){item.append(element('span','',source.title),element('code','source-url',source.locator));const copy=element('button','text-button','複製網址');copy.type='button';copy.addEventListener('click',()=>navigator.clipboard.writeText(source.locator).then(()=>toast('來源網址已複製。')).catch(()=>toast('請選取上方網址複製。')));item.append(copy);}else item.append(element('span','',source.title));item.append(document.createTextNode(' · '+(labels[source.kind]||'參考資料')));list.append(item);}$('background-sources').replaceChildren(list);backgroundSourcesLoaded=true;}catch(error){$('background-sources').textContent='來源暫時無法載入，稍後可重新展開查看。';}});
const replyLabels={brief:'簡短 · 重點回應',balanced:'自然 · 完整說明',detailed:'詳細 · 長文與故事',long:'長篇 · 最高 8K tokens',extended:'延伸長篇 · 最高 16K tokens'};
function modelCapabilities(status){
 if(status.model_capabilities?.profiles)return status.model_capabilities;
 const base={contexts:[8192,16384],thinking_modes:['fast'],response_lengths:['brief','balanced','detailed'],installable:true};
 return {profiles:{'4B':{...base,label:'輕量 · 4B'},'8B':{...base,label:'平衡 · 8B'}},thinking_modes:{fast:{label:'快速',budget_tokens:0}},reply_tokens:{brief:512,balanced:2048,detailed:4096},budget_scope:'combined'};
}
function selectOptions(id,options){
 const select=$(id),signature=JSON.stringify(options);if(select.optionsSignature===signature)return;
 const previous=select.value;select.replaceChildren();
 for(const item of options){const option=element('option','',item.label);option.value=String(item.value);option.disabled=!!item.disabled;select.append(option);}
 select.value=previous;select.optionsSignature=signature;
}
function availableReplies(capabilities,profile,context,mode){
 const p=capabilities.profiles[profile];if(!p)return [];
 const reasoning=Number(capabilities.thinking_modes?.[mode]?.budget_tokens)||0;
 return p.response_lengths.filter(key=>{
  const answer=Number(capabilities.reply_tokens?.[key]);
  return Number.isFinite(answer)&&answer>0&&answer+reasoning+(mode==='fast'?0:128)+256<context;
 });
}
function capabilitiesUI(status){
 const c=modelCapabilities(status),s=status.settings,p=c.profiles[s.profile];
 selectOptions('profile',Object.entries(c.profiles).map(([value,item])=>({value,label:item.label+(!item.installed&&item.installable===false?'（尚未安裝）':''),disabled:!item.installed&&item.installable===false})));
 if(!p)return;
 selectOptions('context-tokens',p.contexts.map(value=>({value,label:`${value/1024}K · ${value<=8192?'較省顯示記憶體':value>=32768?'擴充長文容量':'長文對話'}`})));
 selectOptions('thinking-mode',p.thinking_modes.map(value=>({value,label:c.thinking_modes?.[value]?.label||value})));
 selectOptions('response-length',availableReplies(c,s.profile,Number(s.context_tokens),s.thinking_mode||'fast').map(value=>({value,label:replyLabels[value]||value})));
 $('model-description').textContent=p.description||(p.experimental?'此模型供隔離試用；長文品質與資源需求仍須依本機實測。':'輕量配置較省資源；平衡配置使用較大的本地模型。');
 const reasoning=Number(c.thinking_modes?.[s.thinking_mode]?.budget_tokens)||0,answer=Number(c.reply_tokens?.[s.response_length])||0;
 $('budget-hint').textContent=reasoning?`思考與答覆共用最多 ${(answer+reasoning+128).toLocaleString('zh-TW')} tokens（思考預留 ${reasoning.toLocaleString('zh-TW')}、答覆目標上限 ${answer.toLocaleString('zh-TW')}，另含轉入答覆的預留）；這是合併上限，不保證最終答覆額度。輸入、背景與近期對話另占容量。`:`快速模式不預留思考；答覆上限 ${answer.toLocaleString('zh-TW')} tokens。輸入、背景、近期對話與答覆共用 ${Number(s.context_tokens/1024)}K 容量，篇幅由問題決定。`;
}
function settingsUI(status){backgroundUI(status);capabilitiesUI(status);const s=status.settings;const mapping={address:'address',profile:'profile','response-length':'response_length','thinking-mode':'thinking_mode','context-tokens':'context_tokens','voice-backend':'voice_backend','voice-rate':'voice_rate','greeting-interval':'greeting_interval','quiet-start':'quiet_start','quiet-end':'quiet_end'};for(const [id,key]of Object.entries(mapping))if(document.activeElement!==$(id))$(id).value=s[key]??(key==='thinking_mode'?'fast':'');const toggles={dnd:'dnd','hide-fullscreen':'hide_fullscreen','greetings-enabled':'greetings_enabled','quiet-hours':'quiet_hours','voice-enabled':'voice_enabled'};for(const[id,key]of Object.entries(toggles))$(id).checked=!!s[key];$('toggle-ai').textContent=s.ai_enabled?'卸載 AI，釋放資源':'重新啟用 AI';$('model-state').textContent=status.engine.loading?'正在載入本地模型…':status.engine.running?`${status.engine.profile} · ${status.engine.context_tokens/1024}K 已載入；暫時不用時可卸載。`:'目前沒有載入模型。第一則回覆會先準備模型。';const p=status.setup,selected=modelCapabilities(status).profiles[s.profile];$('setup-progress').textContent=p.running?`正在準備 ${p.file||'模型'} · ${Math.round((p.bytes||0)/Math.max(1,p.total)*100)}%`:p.error||'';$('install-model').disabled=p.running||selected?.installable===false;$('install-model').textContent=selected?.installable===false?(selected.installed?'14B 選配模型已就緒':'14B 模型尚未安裝'):'準備此配置與語音模型';$('install-model').title=selected?.installable===false?(selected.installed?'14B 已在本機就緒，不需重複下載；首次使用會核對完整性。':'14B 尚未安裝，需先使用離線準備工具取得固定版本並核對。'):'';$('voice-state').textContent=(status.voice_available?'朗讀：'+(status.voice?.label||'本機語音')+'。':'選定的朗讀語音尚未準備好，可改選其他語音。')+(status.recognition_available?'按住說話，放開後辨識並填入輸入框；可編輯後再送出。':'語音辨識尚未準備好，可先輸入文字。');}
async function patch(value){state.settings=await api('/api/settings',value);await pollStatus();}
$('save-address').addEventListener('click',()=>patch({address:$('address').value}).then(()=>toast('稱呼已保存，接下來會重新建立對話。')).catch(fail));
function configurationPatch(status,key,value){
 const c=modelCapabilities(status),next={...status.settings,[key]:value},result={[key]:value},p=c.profiles[next.profile];
 if(!p)throw new Error('此模型配置目前尚未提供。');
 if(key==='profile'&&!p.installed&&p.installable===false)throw new Error('此選配模型尚未安裝，請先使用離線準備工具核對。');
 if(!p.contexts.includes(Number(next.context_tokens))){next.context_tokens=p.contexts.includes(8192)?8192:p.contexts[0];result.context_tokens=next.context_tokens;}
 if(!p.thinking_modes.includes(next.thinking_mode||'fast')){next.thinking_mode='fast';result.thinking_mode='fast';}
 const replies=availableReplies(c,next.profile,Number(next.context_tokens),next.thinking_mode||'fast');
 if(!replies.includes(next.response_length)){next.response_length=replies.includes('balanced')?'balanced':replies[0];if(!next.response_length)throw new Error('此思考與容量組合無法保留答覆空間，請先提高對話容量。');result.response_length=next.response_length;}
 return result;
}
async function changeConfiguration(key,value){
 try{
  const values=configurationPatch(state,key,value),adjusted=Object.keys(values).filter(item=>item!==key);
  await patch(values);
  $('configuration-adjustment').textContent=adjusted.length?'已依模型能力與共用容量調整'+adjusted.map(item=>({context_tokens:'對話容量',thinking_mode:'思考模式',response_length:'回覆長度'}[item])).join('、')+'。':'';
 }catch(error){fail(error);await pollStatus().catch(()=>{});}
}
for(const[id,key]of Object.entries({profile:'profile','thinking-mode':'thinking_mode','context-tokens':'context_tokens'}))$(id).addEventListener('change',()=>changeConfiguration(key,key==='context_tokens'?Number($(id).value):$(id).value));
for(const[id,key]of Object.entries({'response-length':'response_length','voice-backend':'voice_backend','voice-rate':'voice_rate','greeting-interval':'greeting_interval','quiet-start':'quiet_start','quiet-end':'quiet_end'}))$(id).addEventListener('change',()=>{const v=['voice_rate','greeting_interval'].includes(key)?Number($(id).value):$(id).value;patch({[key]:v}).catch(fail);});
for(const[id,key]of Object.entries({dnd:'dnd','hide-fullscreen':'hide_fullscreen','greetings-enabled':'greetings_enabled','quiet-hours':'quiet_hours','voice-enabled':'voice_enabled'}))$(id).addEventListener('change',()=>patch({[key]:$(id).checked}).catch(fail));
$('toggle-ai').addEventListener('click',()=>patch({ai_enabled:!state.settings.ai_enabled}).catch(fail));
$('install-model').addEventListener('click',()=>api('/api/setup',{profile:$('profile').value}).then(()=>pollStatus()).catch(fail));
async function exportBackup(){
 if(backupPending)return;backupPending=true;$('export-backup').disabled=true;$('backup-captures').disabled=true;$('backup-state').textContent='正在建立備份，請保持視窗開啟…';
 try{const result=await api('/api/backup/export',{include_captures:$('backup-captures').checked});const s=result.summary,c=s.counts;
 $('backup-summary').textContent=`${result.filename} · 對話 ${c.messages} 筆、記憶 ${c.memories} 筆、提醒 ${c.reminders} 筆、設定 ${c.settings} 筆；透明截圖 ${s.captures.count} 張。`;
 $('backup-path').value=result.path;$('backup-result').hidden=false;$('backup-state').textContent='備份已完成。請將 ZIP 複製到安全的位置保存。';
 }catch(error){$('backup-state').textContent=error.message||'備份未完成，請稍後重試。';}finally{backupPending=false;$('export-backup').disabled=false;$('backup-captures').disabled=false;}
}
$('export-backup').addEventListener('click',exportBackup);
$('copy-backup-path').addEventListener('click',()=>navigator.clipboard.writeText($('backup-path').value).then(()=>toast('備份檔案位置已複製。')).catch(()=>{$('backup-path').focus();$('backup-path').select();toast('請複製已選取的檔案位置。');}));
async function memories(){const list=await api('/api/memories');$('memory-list').replaceChildren();if(!list.length)$('memory-list').append(element('p','empty','還沒有長期記憶。想留下什麼，再明確告訴我就好。'));for(const row of list){const entry=element('div','entry');entry.append(element('p','',row.text),element('small','',new Date(row.updated*1000).toLocaleString('zh-TW')));const actions=element('div','entry-actions');const edit=element('button','','修改');edit.addEventListener('click',()=>{editingMemory=row.id;$('memory-edit').value=row.text;$('memory-dialog').showModal();$('memory-edit').focus();});const remove=element('button','danger','刪除');remove.addEventListener('click',async()=>{if(!confirm('刪除這筆明確記憶？之後會重新建立對話上下文。'))return;try{await api('/api/memory/delete',{id:row.id});await memories();toast('記憶已刪除，後續對話不會沿用舊上下文。');}catch(error){fail(error);}});actions.append(edit,remove);entry.append(actions);$('memory-list').append(entry);}}
$('memory-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/api/memory/save',{text:$('memory-text').value});$('memory-text').value='';await memories();toast('記憶已保存。');}catch(error){fail(error);}});
$('memory-edit-form').addEventListener('submit',async event=>{event.preventDefault();try{await api('/api/memory/save',{id:editingMemory,text:$('memory-edit').value});$('memory-dialog').close();await memories();toast('記憶已更新。');}catch(error){fail(error);}});
$('memory-edit-cancel').addEventListener('click',()=>$('memory-dialog').close());
$('clear-memories').addEventListener('click',async()=>{if(!confirm('清空全部長期記憶？對話紀錄會保留，但後續上下文會重新建立。'))return;try{await api('/api/memory/delete',{});await memories();toast('全部長期記憶已清空。');}catch(error){fail(error);}});
async function reminders(){const list=await api('/api/reminders');$('reminder-list').replaceChildren();if(!list.length)$('reminder-list').append(element('p','empty','還沒有提醒。先替今天留一個小小的安排吧。'));for(const row of list){const entry=element('div','entry');entry.append(element('p','',row.title),element('small','',`${new Date(row.due*1000).toLocaleString('zh-TW')} · ${row.status==='done'?'已完成':row.status==='due'?'已到期':'待提醒'}`));const actions=element('div','entry-actions');for(const[action,label]of [['done','完成'],['snooze','延後五分鐘'],['delete','刪除']]){if(row.status==='done'&&action!=='delete')continue;const button=element('button',action==='delete'?'danger':'',label);button.addEventListener('click',async()=>{if(action==='delete'&&!confirm('刪除這則提醒？'))return;try{await api('/api/reminder/change',{id:row.id,action});await reminders();}catch(error){fail(error);}});actions.append(button);}entry.append(actions);$('reminder-list').append(entry);}}
$('reminder-form').addEventListener('submit',async event=>{event.preventDefault();try{const minutes=Number($('reminder-minutes').value);if(!Number.isFinite(minutes)||minutes<1||minutes>10080)throw new Error('請輸入 1～10080 分鐘。');await api('/api/reminder/add',{title:$('reminder-title').value,due:Date.now()/1000+minutes*60});$('reminder-title').value='';await reminders();toast('提醒已保存。');}catch(error){fail(error);}});
async function renderHistory(append=false){
 const rows=await api('/api/history'+(append&&historyBefore?'?before='+historyBefore:''));if(!append)$('history-list').replaceChildren();
 if(!rows.length&&!append)$('history-list').append(element('p','empty','還沒有對話紀錄。'));
 for(const row of [...rows].reverse()){const entry=element('div','entry');entry.append(element('small','',`${row.role==='user'?'你':'灰姑娘'} · ${new Date(row.created*1000).toLocaleString('zh-TW')}${row.status==='cancelled'?' · 已停止':row.status==='error'?' · 未完成':''}`),element('p','',row.text));$('history-list').append(entry);}
 if(rows.length)historyBefore=rows[0].id;$('load-history').hidden=rows.length<200;
}
$('load-history').addEventListener('click',async()=>{$('load-history').disabled=true;try{await renderHistory(true);}catch(error){fail(error);}finally{$('load-history').disabled=false;}});
$('clear-history').addEventListener('click',async()=>{if(!confirm('清空對話紀錄？明確保存的長期記憶會保留。'))return;try{await api('/api/history/clear',{});await renderHistory();await history();toast('對話紀錄已清空。');}catch(error){fail(error);}});
async function pollStatus(){const next=await api('/api/status');if(next.quiet&&!state?.quiet)stopSpeech();state=next;const engine=next.engine;const label=engine.loading?'準備本地模型':engine.running?`${engine.profile} · 就緒`:next.settings.ai_enabled?'本地 · 按需載入':'AI 已卸載';if($('status-badge').textContent!==label)$('status-badge').textContent=label;$('quiet-hint').textContent=next.quiet?'安靜模式 · 主動通知暫停':'只在你的電腦裡對話';if(page==='settings')settingsUI(next);$('notice').hidden=!next.error;if(next.error)$('notice').textContent=next.error;}
async function pollEvents(){const events=await api('/api/events?after='+lastEvent);for(const event of events){lastEvent=Math.max(lastEvent,event.id);toast(event.text);}if(events.length&&page==='reminders')await reminders();}
async function getAudioContext(){if(!audioContext)audioContext=new AudioContext();if(audioContext.state==='suspended')await audioContext.resume();return audioContext;}
function stopSpeech(){speechNonce++;speechPending=false;const source=audioSource;audioSource=null;clearInterval(audioTimer);audioTimer=null;if(source)try{source.stop();}catch{}if(speechId){const old=speechId;speechId=null;api('/api/playback',{id:old,mouth:0}).catch(()=>{});}controls();}
async function speak(messageId,automatic=false){
 stopSpeech();const nonce=speechNonce;speechPending=true;controls();
 try{const context=await getAudioContext();toast('正在準備朗讀…');const result=await api('/api/speech',{message_id:messageId,automatic});if(result.skipped||nonce!==speechNonce)return;
 const bytes=await(await request('/api/audio/'+result.id)).arrayBuffer();const buffer=await context.decodeAudioData(bytes);if(nonce!==speechNonce)return;
 const source=context.createBufferSource(),analyser=context.createAnalyser();source.buffer=buffer;analyser.fftSize=512;source.connect(analyser);analyser.connect(context.destination);audioSource=source;speechId=result.id;
 source.onended=()=>{if(audioSource===source)stopSpeech();};source.start();const samples=new Float32Array(analyser.fftSize);let pending=false;
 audioTimer=setInterval(async()=>{if(pending||!speechId)return;analyser.getFloatTimeDomainData(samples);const rms=Math.sqrt(samples.reduce((sum,x)=>sum+x*x,0)/samples.length);pending=true;try{await api('/api/playback',{id:speechId,mouth:Math.min(.9,rms*8)});}catch{}finally{pending=false;}},100);
 }finally{if(nonce===speechNonce)speechPending=false;controls();}
}
function waveBytes(chunks){const length=chunks.reduce((sum,x)=>sum+x.length,0),buffer=new ArrayBuffer(44+length*2),view=new DataView(buffer);const text=(at,value)=>{for(let i=0;i<value.length;i++)view.setUint8(at+i,value.charCodeAt(i));};text(0,'RIFF');view.setUint32(4,36+length*2,true);text(8,'WAVE');text(12,'fmt ');view.setUint32(16,16,true);view.setUint16(20,1,true);view.setUint16(22,1,true);view.setUint32(24,16000,true);view.setUint32(28,32000,true);view.setUint16(32,2,true);view.setUint16(34,16,true);text(36,'data');view.setUint32(40,length*2,true);let offset=44;for(const chunk of chunks)for(const sample of chunk){view.setInt16(offset,Math.max(-32768,Math.min(32767,Math.round(sample*32767))),true);offset+=2;}return buffer;}
function recordingUI(){const on=!!recording;$('microphone').classList.toggle('recording',on);$('microphone').textContent=on?'錄音中…':'按住說話';$('record-hint').textContent=on?'放開後辨識，最多 30 秒':recordStarting?'正在準備麥克風…':'Enter 送出 · Shift+Enter 換行';controls();}
async function releaseRecording(value){
 value.cancelled=true;clearTimeout(value.timer);
 // Stop tracks even when getUserMedia resolves after an earlier cancellation.
 if(value.stream){value.stream.removeEventListener('inactive',value.interrupted);for(const track of value.stream.getTracks()){track.removeEventListener('ended',value.interrupted);try{track.stop();}catch{}}}
 if(value.context&&value.contextChanged)value.context.removeEventListener('statechange',value.contextChanged);
 if(value.node)value.node.port.onmessage=null;
 for(const node of [value.source,value.node,value.silence])if(node)try{node.disconnect();}catch{}
 if(value.context&&!value.closing)value.closing=Promise.resolve().then(()=>value.context.state==='closed'?undefined:value.context.close()).catch(()=>{});
 if(value.closing)await value.closing;
}
function interruptRecording(value){
 if(value.cancelled||(recording!==value&&recordStarting!==value))return;
 recordNonce++;asrPending=false;stopRecording(false,value).catch(fail);toast('錄音已中斷，麥克風或音訊暫時無法使用；重新按住即可再試。');
}
async function startRecording(){
 if(recordWanted||recording||recordStarting)return;
 recordWanted=true;asrPending=false;const value={nonce:++recordNonce,chunks:[],cancelled:false};recordStarting=value;
 const wanted=()=>!value.cancelled&&recordWanted&&value.nonce===recordNonce&&recordStarting===value;
 stopSpeech();recordingUI();
 try{
  await api('/api/cancel',generation?{id:generation}:{}).catch(()=>{});if(!wanted())return;
  value.stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
  if(!wanted())return;
  value.interrupted=()=>interruptRecording(value);value.stream.addEventListener('inactive',value.interrupted);
  for(const track of value.stream.getTracks())track.addEventListener('ended',value.interrupted);
  if(!value.stream.getAudioTracks().some(track=>track.readyState==='live'))throw new Error('麥克風已中斷，請重新連接後再試。');
  value.context=new AudioContext({sampleRate:16000});
  if(value.context.sampleRate!==16000)throw new Error('目前音訊裝置無法使用 16 kHz 錄音，請更換裝置後再試。');
  await value.context.audioWorklet.addModule('/recorder.js');if(!wanted())return;
  value.source=value.context.createMediaStreamSource(value.stream);value.node=new AudioWorkletNode(value.context,'capture-pcm');value.silence=value.context.createGain();value.silence.gain.value=0;
  value.source.connect(value.node);value.node.connect(value.silence);value.silence.connect(value.context.destination);
  value.node.port.onmessage=event=>{if(!value.cancelled)value.chunks.push(event.data);};
  if(value.context.state==='suspended')await value.context.resume();if(!wanted())return;
  if(value.context.state!=='running')throw new Error('音訊暫時無法啟動，請重新按住說話。');
  value.contextChanged=()=>{if(value.context.state!=='running')interruptRecording(value);};value.context.addEventListener('statechange',value.contextChanged);
  recordStarting=null;recording=value;value.started=Date.now();recordingUI();
  value.timer=setTimeout(()=>stopRecording(true,value).catch(fail),30000);
 }catch(error){if(!value.cancelled&&value.nonce===recordNonce)toast(error.name==='NotAllowedError'?'麥克風尚未允許使用；可以先繼續文字對話。':error.message);}
 finally{
  if(recording!==value){if(recordStarting===value){recordStarting=null;recordWanted=false;}await releaseRecording(value);recordingUI();}
 }
}
async function stopRecording(recognize=true,expected=null){
 const value=recording||recordStarting;if(!value||(expected&&expected!==value))return;
 const captured=recording===value;recordWanted=false;if(captured)recording=null;if(recordStarting===value)recordStarting=null;
 recordingUI();await releaseRecording(value);
 if(!recognize||!captured||value.nonce!==recordNonce||Date.now()-value.started<200)return;
 toast('正在本地辨識…');asrPending=true;controls();
 try{const result=await(await request('/api/recognize',waveBytes(value.chunks),true)).json();if(value.nonce!==recordNonce)return;$('composer').value=($('composer').value.trim()?$('composer').value+'\n':'')+result.text;composerCount();$('composer').focus();toast('已填入輸入框，可編輯後送出。');}
 catch(error){if(value.nonce===recordNonce)fail(error);}finally{if(value.nonce===recordNonce)asrPending=false;controls();}
}
$('microphone').addEventListener('pointerdown',event=>{event.preventDefault();$('microphone').focus();$('microphone').setPointerCapture(event.pointerId);startRecording();});
$('microphone').addEventListener('pointerup',()=>{recordWanted=false;stopRecording(true).catch(fail);});$('microphone').addEventListener('pointercancel',()=>{recordWanted=false;stopRecording(false).catch(fail);});
$('microphone').addEventListener('keydown',event=>{if(event.code==='Space'&&!event.repeat){event.preventDefault();startRecording();}});$('microphone').addEventListener('keyup',event=>{if(event.code==='Space'){event.preventDefault();recordWanted=false;stopRecording(true).catch(fail);}});
$('microphone').addEventListener('blur',()=>{recordWanted=false;stopRecording(false).catch(()=>{});});
window.addEventListener('blur',()=>{recordWanted=false;stopRecording(false).catch(()=>{});});
document.addEventListener('visibilitychange',()=>{if(document.hidden){recordWanted=false;stopRecording(false).catch(()=>{});}});
navigator.mediaDevices?.addEventListener('devicechange',()=>{const value=recording||recordStarting;if(value?.stream&&!value.stream.getAudioTracks().some(track=>track.readyState==='live'))interruptRecording(value);});
window.addEventListener('beforeunload',()=>{recordWanted=false;stopRecording(false).catch(()=>{});stopSpeech();});
async function initialize(){try{await pollStatus();await history();const old=await api('/api/events?after=0');lastEvent=old.length?old[old.length-1].id:0;}catch(error){$('notice').hidden=false;$('notice').textContent=error.message;}setInterval(()=>pollStatus().catch(()=>{$('status-badge').textContent='本地服務未連線';}),2000);setInterval(()=>pollEvents().catch(()=>{}),2000);}
initialize();

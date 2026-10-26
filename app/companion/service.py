"""Local companion gateway. No cloud fallbacks, arbitrary commands, or inferred memories."""
import argparse
import datetime
import http.server
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.parse
import uuid
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from engine import Engine,Cancelled
from install_models import Installer
from store import Store
from voice import Voice,traditional
from limits import INPUT_CHARACTERS,REPLY_TOKENS,REPLY_GUIDANCE,generation_budget,validate_settings
from model_capabilities import choices
from background import Background
from dialogue import dialogue_mode,turn_guidance,authored_examples
from profile_guard import ProfileLease
from profile_transfer import create_backup

BASE=Path(__file__).resolve().parent
EMOTIONS=['basic','smile','delight','amazed','sadness','angry','fury','fear','pain','feeling_empty']

def atomic(path,text):
    temp=path.with_name(path.name+'.tmp');temp.write_text(text,encoding='utf-8')
    for attempt in range(20):
        try:os.replace(temp,path);return
        except PermissionError:
            if attempt==19:raise
            time.sleep(.005)

def locate(root,pattern):
    paths=list(root.glob(pattern)) if root.exists() else []
    return paths[0] if paths else root/'not-installed.exe'

def character_context(rows,text):
    """Omit affirmative legacy self-intro sentences from ordinary role context.

    Saved history remains intact. Technical questions keep the original context
    so a role preference cannot hide actual deployment/capability information.
    Quoted narrative, negated labels, user turns and later facts stay unchanged.
    """
    if re.search(r'技術|模型|程式|真實|現實|桌面|視窗|螢幕|電腦|運行|本地|本機|聯網|虛擬|桌寵|人工智慧|功能|(?<![A-Za-z])AI(?![A-Za-z])',text,re.IGNORECASE):
        return rows
    intro=re.compile(r'^\s*(?:王子[，、,\s]*)?(我是|我只是|作為|身為)\s*')
    legacy=re.compile(r'虛擬(?:的)?(?:桌寵|角色)|本機桌面角色|人工智慧助理|AI\s*助理|(?P<adapted>以.{0,25}灰姑娘.{0,8}為原型)',re.IGNORECASE)

    def quoted_positions(value):
        pairs={'「':'」','『':'』','“':'”','‘':'’'};stack=[];quoted=[]
        for index,char in enumerate(value[:180]):
            inside=bool(stack)
            if char in pairs:stack.append(pairs[char]);inside=True
            elif stack and char==stack[-1]:stack.pop();inside=True
            elif char in ('"',"'") and not (char=="'" and index and index+1<len(value) and value[index-1].isascii() and value[index-1].isalpha() and value[index+1].isascii() and value[index+1].isalpha()):
                if stack and stack[-1]==char:stack.pop()
                else:stack.append(char)
                inside=True
            quoted.append(inside)
        return quoted

    def affirmative(sentence,offset,quoted):
        start=intro.match(sentence)
        if not start:return False
        rest=sentence[start.end():]
        # A copular nominal clause may classify the speaker. Later clauses such
        # as "I said that character..." do not classify the speaker themselves.
        clause=re.split(r'[，,：:；;]',rest,maxsplit=1)[0]
        if re.match(r'(?:說|想說|講|提到|描述|引用|轉述|指的是)',clause.strip()):return False
        target=legacy.search(clause)
        if not target:return False
        before=clause[:target.start()]
        if re.search(r'不是|並非|並不|不再|不會|不把|不將|不稱|不叫|不算|不屬|不等於|沒有|否認|非$',before):return False
        # "I am a virtual character's friend" is a relation, not this identity.
        if not target.group('adapted') and re.match(r'\s*(?:的|很|說|回答|寫|走|正在)',clause[target.end():]):return False
        end=offset+start.end()+target.end()
        return end<=180 and not any(quoted[offset:end])

    selected=[]
    for row in rows:
        if row['role']!='assistant':selected.append(row);continue
        raw=row['text'];tag=re.match(r'^\[[a-z_]+\]\s*',raw)
        prefix=raw[:tag.end()] if tag else '';body=raw[len(prefix):]
        quoted=quoted_positions(body);pieces=[];removed=False
        for sentence in re.finditer(r'[^。！？.!?\r\n]*(?:[。！？.!?]+|[\r\n]+|$)',body):
            if sentence.start()==sentence.end():continue
            if sentence.start()>=180:
                pieces.append(body[sentence.start():]);break
            if affirmative(sentence.group(),sentence.start(),quoted):removed=True
            else:pieces.append(sentence.group())
        if not removed:selected.append(row);continue
        remaining=''.join(pieces)
        if remaining.strip():selected.append({**row,'text':prefix+remaining})
    return selected

class Companion:
    def __init__(self,data,app_root,models=None):
        self.data=Path(data).resolve();self.data.mkdir(parents=True,exist_ok=True);self.app_root=Path(app_root).resolve()
        self.profile_lease=ProfileLease(self.data)
        project=BASE.parents[1]
        candidates=[self.app_root/'models',self.app_root.parent.parent/'models/companion',project/'models/companion',self.data/'models']
        self.models=(Path(models) if models else next((p for p in candidates if (p/'Qwen3-4B-Q4_K_M.gguf').exists() or (p/'Qwen3-8B-Q4_K_M.gguf').exists()),self.data/'models')).resolve()
        llama=locate(self.app_root/'backend/llama','**/llama-server.exe')
        if not llama.is_file():llama=locate(project/'.tools','llama-*/**/llama-server.exe')
        whisper=self.app_root/'backend/whisper/AsrHelper.exe'
        if not whisper.is_file():whisper=project/'build/desktop/backend/whisper/AsrHelper.exe'
        speaker=self.app_root/'SpeechHelper.exe'
        if not speaker.is_file():speaker=project/'build/desktop/SpeechHelper.exe'
        self.store=Store(self.data/'companion.sqlite3');self.engine=Engine(llama,self.models,self.data)
        self.verified_optional_models={}
        self.engine.search_dirs=[self.models] if models else [self.models,*[p.resolve() for p in candidates]]
        local_speaker=self.app_root/'backend/tts/LocalTtsHelper.exe'
        if BASE==project/'app/companion' and not local_speaker.is_file():local_speaker=project/'build/desktop/backend/tts/LocalTtsHelper.exe'
        self.voice=Voice(whisper,self.models/'ggml-small-q5_1.bin',speaker,self.data/'voice',local_speaker)
        self.installer=Installer(BASE/'model-manifest.json',self.models)
        self.persona=json.loads((BASE/'persona.json').read_text(encoding='utf-8'))
        self.background=Background(BASE/'character-background/cinderella.json')
        self.token=secrets.token_urlsafe(32);self.port=0;self.origin=''
        self.busy=threading.Lock();self.active=None;self.stopping=threading.Event()
        self.bridge_lock=threading.RLock();self.activity=0;self.emotion=-1;self.emotion_version=0;self.mouth=0.;self.playback_at=0.;self.speech_id='';self.notice=0;self.bubble=''
        self.presence_fullscreen=False;self.presence_idle=0;self.last_bridge='';self.last_bridge_at=0.;self.started=time.time();self.last_request=time.time();self.last_error='';self.metrics=[]
    def quiet(self):
        if self.store.get('dnd') or self.presence_fullscreen:return True
        if self.store.get('quiet_hours'):
            clock=datetime.datetime.now().strftime('%H:%M');start=self.store.get('quiet_start');end=self.store.get('quiet_end')
            if start!=end and ((start<=clock<end) if start<end else (clock>=start or clock<end)):return True
        return False
    def bridge(self):
        with self.bridge_lock:
            quiet=self.quiet();current=time.time()
            mouth=self.mouth if current-self.playback_at<.7 else 0
            activity=5 if self.speech_id and current-self.playback_at<.7 else self.activity
            value=f'{activity} {self.emotion} {self.emotion_version} {mouth:.4f} {int(quiet)} {self.notice} {int(self.store.get("hide_fullscreen"))}\n{self.bubble.replace(chr(10)," ")[:140]}'
            if value!=self.last_bridge or current-self.last_bridge_at>=1:
                atomic(self.data/'desktop-state.txt',f'{current:.3f} '+value)
                self.last_bridge=value;self.last_bridge_at=current
    def notify(self,kind,text):
        identifier=self.store.event(kind,text)
        with self.bridge_lock:self.notice=identifier;self.bubble=text
    def heartbeat(self):
        while not self.stopping.wait(.1):
            try:
                if (self.data/'quit-request').exists():self.shutdown();return
                presence=self.data/'presence.txt'
                if presence.exists():
                    parts=presence.read_text(encoding='utf-8').split()
                    if len(parts)>=3 and abs(time.time()-float(parts[0]))<15:self.presence_fullscreen=parts[1]=='1';self.presence_idle=max(0,float(parts[2]))
                if int(time.time()*10)%10==0:
                    due=self.store.due(self.quiet())
                    if due:
                        overdue=any(time.time()-item['due']>60 for item in due)
                        self.notify('reminder',('補發提醒：' if overdue else '到期提醒：')+'；'.join(item['title'] for item in due))
                    self.greeting()
                    if not self.active and self.engine.status()['running'] and time.time()-self.last_request>(60 if self.presence_fullscreen else 300):self.engine.stop()
                self.bridge()
                if self.last_error.startswith('本地狀態更新失敗：'):self.last_error=''
            except Exception as error:self.last_error='本地狀態更新失敗：'+type(error).__name__
    def greeting(self):
        if not self.store.get('greetings_enabled') or self.quiet() or self.active or self.presence_idle<120:return
        last=self.store.get('_last_greeting',self.started)
        if time.time()-last<self.store.get('greeting_interval'):return
        day=datetime.date.today().isoformat();counts=self.store.get('_greeting_day',{})
        if counts.get('day')==day and counts.get('count',0)>=3:return
        lines=['我在這裡。忙完後，也記得讓自己休息一下。','今天也按照自己的步調來吧。','希望你現在有一點舒服的空間。']
        count=counts.get('count',0) if counts.get('day')==day else 0
        self.notify('greeting',self.store.get('address')+'，'+lines[count%len(lines)])
        self.store.set('_last_greeting',time.time());self.store.set('_greeting_day',{'day':day,'count':count+1})
    def status(self):
        voice=self.voice.status(self.store.get('voice_backend'))
        return {'version':'0.3.1','revision':'offline-runtime-20261025','settings':self.store.settings(),'limits':{'input_characters':INPUT_CHARACTERS,'reply_tokens':REPLY_TOKENS},'model_capabilities':choices(self.engine),'engine':self.engine.status(),'background':self.background.status(),'setup':dict(self.installer.progress),'busy':self.active['id'] if self.active else None,'quiet':self.quiet(),'fullscreen':self.presence_fullscreen,'voice_available':voice['available'],'voice':voice,'recognition_available':self.voice.recognizer.is_file() and self.voice.model.is_file(),'error':self.last_error}
    def export_backup(self,body):
        if set(body)-{'include_captures'}:raise ValueError('備份不接受自訂路徑或其他選項')
        captures=body.get('include_captures',False)
        if type(captures) is not bool:raise ValueError('包含透明截圖必須是開關值')
        if not self.busy.acquire(blocking=False):raise ValueError('正在回覆或建立備份，請完成或停止目前回覆後重試')
        try:
            destination=self.data/'backups'/('DesktopWaifu-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]+'.zip')
            with self.store.lock:
                summary=create_backup(self.data.parent,destination,connection=self.store.db,include_captures=captures)
            return {'summary':summary,'path':str(destination),'filename':destination.name}
        finally:self.busy.release()
    def cancel(self,identifier=None):
        active=self.active
        if identifier is not None and (not active or active['id']!=identifier):return False
        if active and (identifier is None or active['id']==identifier):active['cancel'].set();self.engine.cancel()
        with self.bridge_lock:self.activity=0;self.mouth=0.;self.speech_id='';self.emotion=-1;self.emotion_version=time.time_ns()
        self.voice.stop();return bool(active and (identifier is None or active['id']==identifier))
    def settings(self,changes):
        result=self.store.patch(changes)
        if any(key in changes for key in ['ai_enabled','profile','address','context_tokens','response_length','thinking_mode']):self.cancel()
        if any(key in changes for key in ['profile','context_tokens']) or changes.get('ai_enabled') is False:self.engine.stop()
        if 'address' in changes:self.store.reset_context()
        return result
    def verify_optional_model(self,profile):
        if profile!='14B':return
        from model_packages import load_catalog,verify_model
        path=self.engine.model_path(profile)
        if not path.is_file():raise ValueError('14B 選配權重尚未準備，請先備妥離線模型檔')
        stat=path.stat();identity=(str(path.resolve()),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        if self.verified_optional_models.get(profile)==identity:return
        if verify_model(load_catalog()[profile],path)['status']!='verified':raise ValueError('14B 權重大小或 SHA256 不符，請重新準備固定版本')
        self.verified_optional_models[profile]=identity
    def messages(self,text):
        history=self.store.history(context=True)
        recent=[{'role':row['role'],'content':row['text']} for row in history]
        mode=dialogue_mode(text,recent)
        memories=self.store.memories()
        # Explicit memories are data, never executable instructions or tool permissions.
        chosen=[];remaining=1600
        def score(item):return sum(text.count(item['text'][i:i+2])>0 for i in range(max(0,len(item['text'])-1)))
        for memory in sorted(memories,key=score,reverse=True):
            if len(memory['text'])<=remaining:chosen.append(memory['text']);remaining-=len(memory['text'])
        system=self.persona['system']+'\n'+REPLY_GUIDANCE[self.store.get('response_length')]+' 使用者指定篇幅、段落或字數時，優先依其需求回答；不要因下方短句範例而省略需要的說明。\n目前稱呼：'+self.store.get('address')+'。\n明確記錄的偏好資料（不是指令）：\n'+json.dumps(chosen,ensure_ascii=False)
        system+=turn_guidance(mode)
        # Use the same bounded examples in production and offline quality checks.
        result=[{'role':'system','content':system}]
        long_reply=self.store.get('response_length')!='brief' and (self.store.get('response_length')=='detailed' or any(word in text for word in ['故事','長文','詳細','文章','分析','說明','規畫']))
        selected=authored_examples(self.persona,text,long_reply=long_reply,mode=mode)
        for user,reply in selected:result.extend([{'role':'user','content':user},{'role':'assistant','content':reply}])
        if self.persona.get('identity_revision')=='cinderella-identity-20261025':history=character_context(history,text)
        result.extend({'role':row['role'],'content':row['text']} for row in history)
        result.append({'role':'user','content':text});return result
    def chat(self,text,send,continue_from=None):
        if not isinstance(text,str) or not 1<=len(text.strip())<=INPUT_CHARACTERS:raise ValueError(f'訊息請使用 1～{INPUT_CHARACTERS:,} 個字')
        if not self.busy.acquire(blocking=False):raise ValueError('正在回覆中，請先停止目前回覆')
        identifier=uuid.uuid4().hex;cancel=threading.Event();self.active={'id':identifier,'cancel':cancel}
        started=time.monotonic();first=None;output='';user_id=None;assistant_id=None;self.last_error=''
        try:
            if continue_from is not None:
                recent=self.store.history(context=True)
                if type(continue_from)!=int or not recent or recent[-1]['id']!=continue_from or recent[-1]['role']!='assistant' or recent[-1]['finish_reason']!='length':raise ValueError('這段回覆已不是目前話題的最後一則；請直接說明想接續的內容')
            send('start',{'id':identifier});self.activity=4;self.emotion=-1;self.last_request=time.time()
            stripped=text.strip()
            for prefix in ['請記住：','記住：','請記住:','記住:']:
                if stripped.startswith(prefix):
                    memory=stripped[len(prefix):].strip();self.store.save_memory(memory)
                    self.store.message('user',text);reply='已記住。你可以隨時到記憶頁查看、修改或刪除。';message_id=self.store.message('assistant',reply)
                    send('delta',{'text':reply});send('done',{'id':identifier,'message_id':message_id,'memory_saved':True});return
            if not self.store.get('ai_enabled'):raise ValueError('AI 已卸載；可以在設定頁重新啟用，桌寵與提醒仍可使用')
            scope_reply=self.background.coverage_reply(text)
            if scope_reply:
                self.store.message('user',text);message_id=self.store.message('assistant',scope_reply)
                metrics={'first_text_seconds':time.monotonic()-started,'total_seconds':time.monotonic()-started,
                         'characters':len(scope_reply),'model_used':False,'model_load_seconds':0,
                         'profile':self.store.get('profile'),'input_tokens':0,'output_tokens':0,
                         'max_tokens':REPLY_TOKENS[self.store.get('response_length')],
                         'context_tokens':self.store.get('context_tokens'),'trimmed_messages':0,'finish_reason':'stop',
                         'background':{'baseline':self.background.baseline['id'],'entry_ids':['coverage'],
                                       'mode':'coverage','tokens':0,'token_budget':1200,'omitted_entries':0}}
                self.metrics.append(metrics);self.metrics=self.metrics[-100:]
                self.emotion=0;self.emotion_version=time.time_ns();send('emotion',{'name':'basic'})
                send('delta',{'text':scope_reply});send('done',{'id':identifier,'message_id':message_id,
                    'finish_reason':'stop','metrics':metrics});return
            messages=self.messages(text)
            recent=[{'role':row['role'],'content':row['text']} for row in self.store.history(context=True)]
            background=self.background.select(text,recent)
            user_id=self.store.message('user',text,'generating');assistant_id=self.store.message('assistant','','generating')
            pending='';header=True
            settings=self.store.settings();max_tokens=REPLY_TOKENS[settings['response_length']]
            budget=validate_settings(settings)
            self.verify_optional_model(settings['profile'])
            def reasoning(event):
                if cancel.is_set() or not self.active or self.active['id']!=identifier:raise Cancelled()
                if isinstance(event,dict) and event.get('phase') in ('thinking','answer'):
                    elapsed=event.get('elapsed_seconds',0)
                    if type(elapsed) in (int,float) and math.isfinite(elapsed):
                        send('reasoning',{'phase':event['phase'],'elapsed_seconds':max(0,elapsed)})
            for chunk in self.engine.stream(messages,settings['profile'],cancel,settings['context_tokens'],max_tokens,continue_from is not None,background=background,thinking_mode=settings['thinking_mode'],reasoning_callback=reasoning):
                if cancel.is_set() or not self.active or self.active['id']!=identifier:raise Cancelled()
                pending+=chunk
                if header:
                    match=re.match(r'^\s*\[([a-z_]+)\]\s*',pending)
                    if match:
                        emotion=match.group(1);pending=pending[match.end():];header=False
                        if emotion in EMOTIONS:self.emotion=EMOTIONS.index(emotion);self.emotion_version=time.time_ns();send('emotion',{'name':emotion})
                    elif len(pending)>48 or (pending.strip() and not pending.lstrip().startswith('[')):header=False
                    else:continue
                if pending:
                    visible=traditional(pending);pending=''
                    if not output:visible=visible.lstrip()
                    if not visible:continue
                    output+=visible
                    if first is None:first=time.monotonic()-started
                    send('delta',{'text':visible})
            if cancel.is_set():raise Cancelled()
            if pending and not re.fullmatch(r'\s*\[[a-z_]+\]\s*',pending):
                if first is None:first=time.monotonic()-started
                output+=traditional(pending);send('delta',{'text':traditional(pending)})
            if not output.strip():raise RuntimeError('本地模型沒有產生回覆，請再試一次')
            self.store.finish(user_id,text,'complete');self.store.finish(assistant_id,output,'complete',self.engine.finish_reason)
            metrics={'first_text_seconds':first,'total_seconds':time.monotonic()-started,'characters':len(output),'model_load_seconds':self.engine.request_load_seconds,'profile':self.engine.profile,'input_tokens':self.engine.prompt_tokens,'output_tokens':self.engine.completion_tokens,'max_tokens':max_tokens,'context_tokens':self.engine.context_tokens,'trimmed_messages':self.engine.trimmed_messages,'finish_reason':self.engine.finish_reason,'background':self.engine.background_metrics}
            metrics.update({'thinking_mode':settings['thinking_mode'],'combined_max_tokens':budget['combined_tokens'],
                'reasoning_budget_tokens':budget['reasoning_tokens'],'budget_scope':budget['budget_scope'],
                'reasoning_tokens':getattr(self.engine,'reasoning_tokens',None),
                'reasoning_seconds':getattr(self.engine,'reasoning_seconds',0),
                'answer_tokens':getattr(self.engine,'answer_tokens',None),
                'reasoning_tokens_source':getattr(self.engine,'reasoning_tokens_source','unknown'),
                'reasoning_seconds_scope':getattr(self.engine,'reasoning_seconds_scope','unknown'),
                'native_completion_tokens':getattr(self.engine,'native_completion_tokens',None),
                'answer_limit_enforced':getattr(self.engine,'answer_limit_enforced',False)})
            if settings['thinking_mode']!='fast' and getattr(self.engine,'native_completion_tokens',None) is None:
                metrics['output_tokens']=None
            self.store.save_generation_metrics(assistant_id,metrics)
            self.metrics.append(metrics);self.metrics=self.metrics[-100:]
            send('done',{'id':identifier,'message_id':assistant_id,'finish_reason':self.engine.finish_reason,'metrics':metrics})
        except Cancelled:
            if user_id:self.store.finish(user_id,text,'cancelled')
            if assistant_id:self.store.finish(assistant_id,output,'cancelled')
            send('cancelled',{'id':identifier,'message_id':assistant_id})
        except (BrokenPipeError,ConnectionResetError):
            cancel.set();self.engine.cancel()
            if user_id:self.store.finish(user_id,text,'cancelled')
            if assistant_id:self.store.finish(assistant_id,output,'cancelled')
        except Exception as error:
            if user_id:self.store.finish(user_id,text,'error')
            if assistant_id:self.store.finish(assistant_id,output,'error')
            self.last_error=str(error)[:240];send('error',{'id':identifier,'message':self.last_error})
        finally:
            if self.active and self.active['id']==identifier:self.active=None
            self.activity=0;self.mouth=0;self.busy.release()

class Handler(http.server.BaseHTTPRequestHandler):
    server_version='DesktopWaifu-local/0.3'
    def log_message(self,*args):pass
    @property
    def app(self):return self.server.app
    def headers_common(self):
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer')
    def json(self,value,status=200):
        raw=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.headers_common();self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def authorized(self):
        supplied=self.headers.get('X-Waifu-Token','')
        return secrets.compare_digest(supplied,self.app.token) and self.headers.get('Origin',self.app.origin)==self.app.origin
    def do_GET(self):
        parsed=urllib.parse.urlparse(self.path);path=parsed.path
        if path.startswith('/api/'):
            if not self.authorized():self.json({'error':'需要本機工作階段'},403);return
            if path=='/api/status':self.json(self.app.status())
            elif path=='/api/background':self.json(self.app.background.catalog())
            elif path=='/api/history':
                before=urllib.parse.parse_qs(parsed.query).get('before',[None])[0]
                if before is not None and (not before.isdigit() or len(before)>15):self.json({'error':'無效的紀錄位置'},400);return
                self.json(self.app.store.history(before=before))
            elif path=='/api/memories':self.json(self.app.store.memories())
            elif path=='/api/reminders':self.json(self.app.store.reminders())
            elif path=='/api/events':self.json(self.app.store.events(urllib.parse.parse_qs(parsed.query).get('after',['0'])[0]))
            elif path=='/api/voices':self.json(self.app.voice.voices())
            elif path.startswith('/api/audio/'):
                identifier=path.rsplit('/',1)[-1]
                if not re.fullmatch('[a-f0-9]{32}',identifier):self.json({'error':'無效的音訊'},400);return
                audio=self.app.voice.data/(identifier+'.wav')
                if not audio.is_file():self.json({'error':'找不到音訊'},404);return
                raw=audio.read_bytes();self.send_response(200);self.headers_common();self.send_header('Content-Type','audio/wav');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            else:self.json({'error':'找不到項目'},404)
            return
        public={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/recorder.js':'recorder.js','/portrait.png':'portrait.png'}
        if path not in public:self.json({'error':'找不到頁面'},404);return
        file=BASE/'web'/public[path]
        if not file.exists():self.json({'error':'介面準備中'},503);return
        types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.png':'image/png'}
        raw=file.read_bytes();self.send_response(200);self.headers_common();self.send_header('Content-Type',types[file.suffix]);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Content-Security-Policy',"default-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; script-src 'self'; style-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers();self.wfile.write(raw)
    def do_POST(self):
        if not self.authorized():self.json({'error':'需要本機工作階段'},403);return
        path=urllib.parse.urlparse(self.path).path
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=2*1024*1024:raise ValueError('內容過大或沒有內容')
            raw=self.rfile.read(length)
            if path=='/api/recognize':self.json({'text':self.app.voice.recognize(raw)});return
            if self.headers.get_content_type()!='application/json':raise ValueError('需要 JSON 請求')
            body=json.loads(raw)
            if not isinstance(body,dict):raise ValueError('無效的請求內容')
            if path=='/api/chat':
                self.send_response(200);self.headers_common();self.send_header('Content-Type','text/event-stream; charset=utf-8');self.end_headers()
                def send(kind,value):self.wfile.write(('data: '+json.dumps({'type':kind,**value},ensure_ascii=False)+'\n\n').encode());self.wfile.flush()
                try:self.app.chat(body.get('text'),send,body.get('continue_from'))
                except ValueError as error:send('error',{'message':str(error)})
                return
            if path=='/api/cancel':self.json({'cancelled':self.app.cancel(body.get('id'))})
            elif path=='/api/backup/export':self.json(self.app.export_backup(body))
            elif path=='/api/settings':self.json(self.app.settings(body))
            elif path=='/api/setup':self.app.installer.start(body.get('profile','4B'));self.json({'started':True})
            elif path=='/api/memory/save':self.app.cancel();self.json({'id':self.app.store.save_memory(body.get('text'),body.get('id'))})
            elif path=='/api/memory/delete':self.app.cancel();self.app.store.delete_memory(body.get('id'));self.json({'deleted':True,'context_reset':True})
            elif path=='/api/history/clear':self.app.cancel();self.app.voice.clear_cache();self.app.store.clear_history();self.json({'cleared':True})
            elif path=='/api/context/new':self.app.cancel();self.app.store.reset_context();self.json({'reset':True})
            elif path=='/api/reminder/add':self.json({'id':self.app.store.reminder(body.get('title'),body.get('due'))})
            elif path=='/api/reminder/change':self.app.store.change_reminder(body.get('id'),body.get('action'));self.json({'changed':True})
            elif path=='/api/speech':
                if body.get('automatic') and self.app.quiet():self.json({'skipped':True});return
                identifier=int(body.get('message_id',0));message=next((m for m in self.app.store.history() if m['id']==identifier and m['role']=='assistant' and m['status']=='complete'),None)
                if not message:raise ValueError('找不到可朗讀的回覆')
                self.app.cancel(None) if body.get('interrupt') else self.app.voice.stop()
                result=self.app.voice.synthesize(message['text'],self.app.store.get('voice_rate'),self.app.store.get('voice_backend'));self.app.speech_id=result['id'];self.json(result)
            elif path=='/api/playback':
                if body.get('id')==self.app.speech_id:
                    mouth=float(body.get('mouth',0));self.app.mouth=max(0,min(1,mouth)) if math.isfinite(mouth) else 0;self.app.playback_at=time.time()
                self.json({'ok':True})
            elif path=='/api/shutdown':self.app.cancel();self.json({'stopped':True});threading.Thread(target=self.server.shutdown,daemon=True).start()
            else:self.json({'error':'找不到項目'},404)
        except (ValueError,TypeError,KeyError,json.JSONDecodeError) as error:self.json({'error':str(error)},400)
        except Exception as error:self.json({'error':str(error)[:240]},503)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);parser.add_argument('--app-root',default=str(BASE.parent));parser.add_argument('--models');parser.add_argument('--port',type=int,default=0);args=parser.parse_args()
    app=Companion(args.data,args.app_root,args.models);server=http.server.ThreadingHTTPServer(('127.0.0.1',args.port),Handler);server.daemon_threads=True;server.app=app
    app.port=server.server_port;app.origin=f'http://127.0.0.1:{app.port}';app.shutdown=server.shutdown
    connection=f'{app.port}\n{app.token}\n{os.getpid()}\n'
    atomic(app.data/'connection.txt',connection)
    heartbeat=threading.Thread(target=app.heartbeat,daemon=True);heartbeat.start()
    try:server.serve_forever(poll_interval=.2)
    finally:
        app.stopping.set()
        # Withdraw this session before potentially slow model/request cleanup.
        # Do not remove a replacement service's connection information.
        path=app.data/'connection.txt'
        if path.exists() and path.read_text(encoding='utf-8')==connection:path.unlink(missing_ok=True)
        try:
            app.cancel();app.engine.stop()
        finally:
            heartbeat.join(timeout=2);server.server_close()
            deadline=time.monotonic()+5
            while app.active and time.monotonic()<deadline:time.sleep(.05)
            try:app.store.close()
            finally:
                (app.data/'quit-request').unlink(missing_ok=True)
                app.profile_lease.close()

if __name__=='__main__':main()

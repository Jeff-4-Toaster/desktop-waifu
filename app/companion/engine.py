"""Independent llama.cpp process; localhost only, streamed and cancellable."""
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import threading
import time
from limits import generation_budget as output_budget

class Cancelled(Exception):pass

def hidden():return {'creationflags':getattr(subprocess,'CREATE_NO_WINDOW',0)}


def inference_environment():
    # Keep localhost/model/parser choices in the explicit launch arguments.
    return {key:value for key,value in os.environ.items()
            if not key.upper().startswith(('LLAMA_','GGML_RPC_'))}


class FinalContentFilter:
    """Withhold split control tags; raw thinking blocks never become text."""
    def __init__(self):
        self.pending='';self.in_thinking=False;self.filtered_thinking=False
    def feed(self,text,final=False):
        self.pending+=text;parts=[]
        while self.pending:
            tag='</think>' if self.in_thinking else '<think>'
            index=self.pending.find(tag)
            if index>=0:
                if not self.in_thinking:parts.append(self.pending[:index])
                self.pending=self.pending[index+len(tag):]
                self.in_thinking=not self.in_thinking
                self.filtered_thinking=True
                continue
            if not self.in_thinking and '</think>' in self.pending:
                raise RuntimeError('模型未正確分開思考與答覆，請改用快速模式')
            keep=0
            if not final:
                for length in range(1,min(len(self.pending),max(len('<think>'),len('</think>'))-1)+1):
                    if '<think>'.startswith(self.pending[-length:]) or '</think>'.startswith(self.pending[-length:]):keep=length
            safe=self.pending[:-keep] if keep else self.pending
            if not self.in_thinking:parts.append(safe)
            self.pending=self.pending[-keep:] if keep else ''
            break
        return ''.join(parts)

class Engine:
    def __init__(self,executable,models,data):
        self.executable=Path(executable);self.models=Path(models);self.data=Path(data)
        self.search_dirs=[self.models]
        self.process=None;self.profile=None;self.port=0;self.key=secrets.token_hex(24)
        self.lock=threading.RLock();self.connection=None;self.loading=False;self.load_seconds=0;self.request_load_seconds=0;self.prompt_tokens=0
        self.context_tokens=16384;self.trimmed_messages=0;self.completion_tokens=0;self.finish_reason=''
        self.background_metrics={'entry_ids':[],'tokens':0,'token_budget':1200}
        self._backend_capabilities=None;self._capability_stamp=None
        self.thinking_mode='fast';self.reasoning_tokens=None;self.reasoning_seconds=0
        self.answer_tokens=None;self.combined_max_tokens=2048;self.reasoning_max_tokens=0
        self.budget_scope='combined';self.completion_tokens_reported=False
        self.answer_limit_enforced=False;self.token_accounting='backend_combined_usage'
        self.native_completion_tokens=None;self.reasoning_tokens_source='unavailable'
        self.reasoning_seconds_scope='request_to_first_final_content'
    def backend_capabilities(self):
        stamp=(self.executable.stat().st_size,self.executable.stat().st_mtime_ns)
        if self._backend_capabilities is not None and self._capability_stamp==stamp:return dict(self._backend_capabilities)
        result={'thinking':False,'reasoning_budget':False,'reasoning_format':None,
                'budget_scope':'combined','build':None,'verification':'binary_help_and_pinned_schema'}
        try:
            options={'capture_output':True,'timeout':10,'env':inference_environment(),**hidden()}
            version=subprocess.run([str(self.executable),'--version'],**options)
            help_result=subprocess.run([str(self.executable),'--help'],**options)
            if max(len(version.stdout),len(version.stderr),len(help_result.stdout),len(help_result.stderr))>262144:
                raise ValueError('backend capability output too large')
            version_text=(version.stdout+version.stderr).decode('utf-8',errors='replace')
            match=re.search(r'\bbuild\s+(\d+)\b',version_text) or re.search(r'\bversion:\s*b?(\d{4,})(?![\d.])',version_text)
            result['build']='b'+match.group(1) if match else None
            help_text=(help_result.stdout+help_result.stderr).decode('utf-8',errors='replace')
            # These JSON fields are checked against tools/server/server-schema.cpp
            # at the exact b11669 tag, rather than assumed from newer master docs.
            supported=(version.returncode==help_result.returncode==0 and result['build']=='b11669'
                       and all(flag in help_text for flag in ('--reasoning-format','--reasoning-budget','--jinja')))
            result.update({'thinking':supported,'reasoning_budget':supported,
                           'reasoning_format':'deepseek' if supported else None})
        except (OSError,subprocess.SubprocessError,ValueError):pass
        self._backend_capabilities=result;self._capability_stamp=stamp
        return dict(result)
    def _request(self,path,value=None,timeout=10):
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=timeout)
        body=json.dumps(value,ensure_ascii=False).encode() if value is not None else None
        try:
            connection.request('POST' if body is not None else 'GET',path,body,{'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})
            response=connection.getresponse();raw=response.read()
            if response.status!=200:raise RuntimeError('本地模型服務尚未就緒' if response.status==503 else '本地模型請求失敗：'+str(response.status))
            return json.loads(raw)
        finally:connection.close()
    def stop(self):
        self.cancel()
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
            self.process=None;self.profile=None;self.loading=False
    def cancel(self):
        connection=self.connection
        if connection:
            try:
                if connection.sock:connection.sock.shutdown(socket.SHUT_RDWR)
                connection.close()
            except OSError:pass
    def ensure(self,profile,cancel,context_tokens=16384):
        caps={'4B':16384,'8B':32768,'14B':8192}
        if not isinstance(profile,str) or profile not in caps:
            raise ValueError('未知的本地模型配置')
        if type(context_tokens) is not int or context_tokens not in (4096,8192,16384,32768) or context_tokens>caps[profile]:
            raise ValueError('此模型尚未開放所選對話容量')
        with self.lock:
            if self.process and self.process.poll() is None and self.profile==profile and self.context_tokens==context_tokens and not self.loading:
                try:
                    if self._request('/health',timeout=1).get('status')=='ok':return 0
                except (OSError,RuntimeError,http.client.HTTPException):pass
            self.stop()
            self.context_tokens=context_tokens
            model=self.model_path(profile)
            if not model.is_file():raise RuntimeError('尚未安裝本地模型，請在設定頁準備模型')
            if not self.executable.is_file():raise RuntimeError('找不到本地推論執行檔')
            with socket.socket() as reserve:reserve.bind(('127.0.0.1',0));self.port=reserve.getsockname()[1]
            self.data.mkdir(parents=True,exist_ok=True)
            args=[str(self.executable),'-m',str(model),'-c',str(context_tokens),'-ngl','99','-t','6','-b','512','-ub','128','-np','1','--host','127.0.0.1','--port',str(self.port),'--api-key',self.key,'--jinja','--no-webui','--flash-attn','on','--reasoning-format','deepseek','--log-verbosity','1','--log-colors','off']
            with (self.data/'model-service.log').open('ab') as log:self.process=subprocess.Popen(args,cwd=self.executable.parent,stdout=log,stderr=log,env=inference_environment(),**hidden())
            self.profile=profile;self.loading=True;started=time.monotonic()
            try:
                while time.monotonic()-started<150:
                    if cancel.is_set():raise Cancelled()
                    if self.process.poll() is not None:raise RuntimeError('本地模型未能啟動；桌寵仍可使用，請查看設定頁')
                    try:
                        if self._request('/health',timeout=1).get('status')=='ok':
                            self.loading=False;self.load_seconds=time.monotonic()-started;return self.load_seconds
                    except (OSError,RuntimeError,http.client.HTTPException):pass
                    time.sleep(.15)
                raise RuntimeError('模型載入超時，請改用輕量配置')
            except Exception:self.stop();raise
    def count_text(self,text):
        return len(self._request('/tokenize',{'content':text,'add_special':False})['tokens'])
    def fit(self,messages,max_tokens=2048,preserve_recent=False,background=None,cancel=None,thinking_mode='fast',reasoning_budget=None):
        # Count the actual chat template instead of estimating Chinese token sizes.
        reserve=output_budget(max_tokens,thinking_mode,reasoning_budget)
        self.combined_max_tokens=reserve['combined_tokens'];self.reasoning_max_tokens=reserve['reasoning_tokens']
        self.budget_scope=reserve['budget_scope']
        working=[{k:v for k,v in message.items() if k not in ('reasoning_content','reasoning','thinking')} for message in messages];self.trimmed_messages=0
        self.background_metrics={'entry_ids':[],'tokens':0,'token_budget':1200}
        original_system=working[0]['content']
        if background is not None:
            background.limit(self.count_text,cancel)
            working[0]={**working[0],'content':original_system+background.render()}
        budget=self.context_tokens-reserve['combined_tokens']-256
        while True:
            if cancel is not None and cancel.is_set():raise Cancelled()
            prompt=self._request('/apply-template',{'messages':working,'add_generation_prompt':True,'chat_template_kwargs':{'enable_thinking':thinking_mode!='fast'}})['prompt']
            if thinking_mode!='fast' and not prompt.rstrip().endswith('<think>'):
                # The official Qwen3 GGUF starts the assistant turn without an
                # opening tag when thinking is enabled; the model emits it.
                # Verify the actual toggle rather than assuming a prefilled tag.
                disabled=self._request('/apply-template',{'messages':working,'add_generation_prompt':True,
                                       'chat_template_kwargs':{'enable_thinking':False}})['prompt']
                if not (prompt.rstrip().endswith('<|im_start|>assistant')
                        and disabled.rstrip().endswith('</think>') and '<think>' in disabled[-40:]
                        and prompt!=disabled):
                    raise ValueError('此模型的思考模板尚未通過辨識，請改用快速模式')
            count=self.count_text(prompt)
            if count<=budget:
                self.prompt_tokens=count
                if background is not None:self.background_metrics=background.metrics()
                return working
            # Character projection can omit an obsolete assistant introduction,
            # and older imports need not contain perfectly paired messages.
            # Trim complete user turns rather than assuming every turn has two
            # rows; a continuation must retain its preceding user request too.
            protected=len(working)-1
            if preserve_recent:
                protected=next((index for index in range(len(working)-2,0,-1)
                                if working[index].get('role')=='user'),max(1,len(working)-2))
            if protected<=1:
                # Preserve the current question/continuation before optional reference data.
                if background is not None and background.drop_last():
                    working[0]={**working[0],'content':original_system+background.render()};continue
                raise ValueError('輸入、思考與回覆超過目前對話容量，請提高容量、減少篇幅或思考預算；已保存的紀錄仍會保留')
            end=next((index for index in range(2,protected)
                      if working[index].get('role')=='user'),protected)
            self.trimmed_messages+=end-1
            del working[1:end]
    def stream(self,messages,profile,cancel,context_tokens=16384,max_tokens=2048,preserve_recent=False,background=None,thinking_mode='fast',reasoning_callback=None,reasoning_budget=None):
        reserve=output_budget(max_tokens,thinking_mode,reasoning_budget)
        if cancel.is_set():raise Cancelled()
        if reasoning_callback is not None and not callable(reasoning_callback):raise ValueError('思考狀態回呼不正確')
        self.completion_tokens=0;self.finish_reason='';self.completion_tokens_reported=False
        self.thinking_mode=thinking_mode;self.reasoning_tokens=None;self.reasoning_seconds=0;self.answer_tokens=None
        self.reasoning_max_tokens=reserve['reasoning_tokens'];self.combined_max_tokens=reserve['combined_tokens']
        self.budget_scope='combined';self.answer_limit_enforced=False;self.native_completion_tokens=None
        self.reasoning_tokens_source='unavailable';self.token_accounting='backend_combined_usage_and_retokenized_visible_text'
        if thinking_mode!='fast':
            capabilities=self.backend_capabilities()
            if profile not in ('4B','8B','14B') or not capabilities.get('thinking') or not capabilities.get('reasoning_budget'):
                raise ValueError('此本地後端尚未驗證思考與預算控制，請改用快速模式')
        # Retry a dead/stale listener only before any response has been emitted.
        for attempt in range(2):
            try:
                self.request_load_seconds=self.ensure(profile,cancel,context_tokens)
                messages=self.fit(messages,max_tokens,preserve_recent,background,cancel,thinking_mode=thinking_mode,reasoning_budget=reasoning_budget);break
            except (OSError,http.client.HTTPException,RuntimeError):
                if cancel.is_set():raise Cancelled()
                self.stop()
                if attempt:raise
                if cancel.wait(.15):raise Cancelled()
        if cancel.is_set():raise Cancelled()
        connection=http.client.HTTPConnection('127.0.0.1',self.port,timeout=300);self.connection=connection
        temperature=.25 if background is not None and background.mode=='reference' else .7
        body={'messages':messages,'stream':True,'max_tokens':reserve['combined_tokens'],'temperature':temperature,'top_p':.85,'presence_penalty':.25,'chat_template_kwargs':{'enable_thinking':thinking_mode!='fast'},'reasoning_format':'deepseek','stream_options':{'include_usage':True},'cache_prompt':False}
        if thinking_mode!='fast':
            # Official Qwen3-8B-GGUF pinned model-card recommendations.
            body.update({'temperature':.6,'top_p':.95,'top_k':20,'min_p':0.,'presence_penalty':1.5,
                         'reasoning_budget_tokens':reserve['reasoning_tokens'],
                         'reasoning_budget_start_tag':'<think>','reasoning_budget_end_tags':['</think>'],
                         'reasoning_budget_message':''})
        self.sampling={key:body[key] for key in ('temperature','top_p','top_k','min_p','presence_penalty') if key in body}
        content_filter=FinalContentFilter();answer='';reasoning=[];reasoning_size=0
        started=time.monotonic();answer_started=False;last_callback=-1.
        def phase(name,force=False):
            nonlocal last_callback
            elapsed=time.monotonic()-started
            if name=='thinking':self.reasoning_seconds=elapsed
            if reasoning_callback is not None and (force or elapsed-last_callback>=.25):
                reasoning_callback({'phase':name,'elapsed_seconds':elapsed,
                                    'reasoning_budget_tokens':reserve['reasoning_tokens']})
                last_callback=elapsed
        def count_reasoning():
            if reasoning:
                self.reasoning_tokens=self.count_text(''.join(reasoning))
                self.reasoning_tokens_source='retokenized_reasoning_content_excludes_control_tokens'
                reasoning.clear()
            elif self.reasoning_tokens is None:
                self.reasoning_tokens=0
                self.reasoning_tokens_source='no_reasoning_content_received'
        def visible(piece):
            nonlocal answer,answer_started
            if not piece:return ''
            if not answer_started:
                answer_started=True
                self.reasoning_seconds=time.monotonic()-started if thinking_mode!='fast' else 0
                count_reasoning();phase('answer',True)
                if cancel.is_set():raise Cancelled()
            if thinking_mode=='fast':
                answer+=piece;return piece
            # llama.cpp's n_predict is a combined cap. This separate local cap
            # uses the same tokenizer and never promises a backend answer quota.
            count=self.count_text(answer+piece)
            if count>max_tokens:
                low,high=0,len(piece)
                while low<high:
                    middle=(low+high+1)//2
                    if self.count_text(answer+piece[:middle])<=max_tokens:low=middle
                    else:high=middle-1
                piece=piece[:low];count=self.count_text(answer+piece)
                self.answer_limit_enforced=True
            answer+=piece;self.answer_tokens=count
            if count>=max_tokens:self.answer_limit_enforced=True
            return piece
        try:
            if thinking_mode!='fast':phase('thinking',True)
            if cancel.is_set():raise Cancelled()
            connection.request('POST','/v1/chat/completions',json.dumps(body,ensure_ascii=False).encode(),{'Authorization':'Bearer '+self.key,'Content-Type':'application/json'})
            response=connection.getresponse()
            if response.status!=200:raise RuntimeError('本地模型無法完成這則回覆，請稍後再試')
            while True:
                if cancel.is_set():raise Cancelled()
                line=response.readline(1024*1024+1)
                if cancel.is_set():raise Cancelled()
                if not line:break
                if len(line)>1024*1024:raise RuntimeError('本地回覆事件超過可接受大小')
                if not line.startswith(b'data:'):continue
                raw=line[5:].strip()
                if raw==b'[DONE]':break
                event=json.loads(raw)
                if event.get('usage'):
                    total=event['usage'].get('completion_tokens')
                    if type(total) is int and total>=0:
                        self.completion_tokens=total;self.native_completion_tokens=total;self.completion_tokens_reported=True
                choices=event.get('choices') or []
                if choices:
                    if choices[0].get('finish_reason'):self.finish_reason=choices[0]['finish_reason']
                    delta=choices[0].get('delta',{})
                    thought=delta.get('reasoning_content') or ''
                    text=delta.get('content') or ''
                    if not isinstance(thought,str) or not isinstance(text,str):raise RuntimeError('本地回覆事件格式不正確')
                    if thought:
                        if answer_started:raise RuntimeError('本地思考與答覆順序不正確')
                        reasoning_size+=len(thought)
                        if reasoning_size>1024*1024:raise RuntimeError('本地思考超過安全容量，請改用快速模式')
                        reasoning.append(thought)
                        if thinking_mode!='fast':phase('thinking')
                    piece=visible(content_filter.feed(text))
                    if piece:yield piece
                    if self.answer_limit_enforced:
                        self.finish_reason='length';break
            if not self.answer_limit_enforced:
                piece=visible(content_filter.feed('',final=True))
                if piece:yield piece
            if not self.finish_reason:raise RuntimeError('回覆連線提早結束，請重新送出或要求接續內容')
            if thinking_mode!='fast' and not answer.strip():
                raise RuntimeError('思考已停止，但尚未產生最終答覆；請降低思考預算或改用快速模式後重試')
        except (OSError,http.client.HTTPException):
            if cancel.is_set():raise Cancelled()
            raise RuntimeError('本地模型連線中斷，桌寵操作不受影響')
        finally:
            if self.connection is connection:self.connection=None
            connection.close()
            if thinking_mode!='fast' and not answer_started:self.reasoning_seconds=time.monotonic()-started
            if not cancel.is_set():
                try:
                    count_reasoning()
                    if self.answer_tokens is None:self.answer_tokens=self.count_text(answer) if answer else 0
                except (OSError,RuntimeError,http.client.HTTPException):pass
            if content_filter.filtered_thinking:
                self.reasoning_tokens=None;self.reasoning_tokens_source='unparsed_thinking_removed_count_unavailable'
            reasoning.clear()
    def status(self):
        running=bool(self.process and self.process.poll() is None)
        return {'running':running,'loading':self.loading,'profile':self.profile if running else None,'context_tokens':self.context_tokens if running else None,'load_seconds':self.load_seconds,'pid':self.process.pid if running else None,'installed':{size:self.model_path(size).is_file() for size in ['4B','8B','14B']},'thinking_mode':self.thinking_mode,'backend_capabilities':self._backend_capabilities}
    def model_path(self,profile):
        name=f'Qwen3-{profile}-Q4_K_M.gguf'
        return next((path/name for path in self.search_dirs if (path/name).is_file()),self.models/name)

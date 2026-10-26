"""Local WAV recognition and Windows/bundled CPU synthesis."""
import array
import ctypes
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
import wave
from engine import hidden
from local_tts import probe as probe_local_tts

def traditional(text):
    if os.name!='nt' or not text:return text
    convert=ctypes.windll.kernel32.LCMapStringEx
    convert.argtypes=[ctypes.c_wchar_p,ctypes.c_uint,ctypes.c_wchar_p,ctypes.c_int,ctypes.c_wchar_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_longlong]
    count=convert('zh-TW',0x04000000,text,len(text),None,0,None,None,0)
    if not count:return text
    buffer=ctypes.create_unicode_buffer(count+1)
    return buffer.value if convert('zh-TW',0x04000000,text,len(text),buffer,count,None,None,0) else text

class Voice:
    def __init__(self,recognizer,model,speaker,data,local_speaker=None):
        self.recognizer=Path(recognizer);self.model=Path(model);self.speaker=Path(speaker)
        self.data=Path(data);self.data.mkdir(parents=True,exist_ok=True)
        self.lock=threading.Lock();self.process=None;self.process_lock=threading.Lock();self.cancel_epoch=0
        self.local_speaker=Path(local_speaker) if local_speaker else self.speaker.parent/'backend/tts/LocalTtsHelper.exe'
        try:self.windows_available=any('404' in [x.strip().lstrip('0').lower() for x in v.get('language','').split(';')] for v in self.voices())
        except Exception:self.windows_available=False
        try:self.local_available=probe_local_tts(self.local_speaker)['available']
        except Exception:self.local_available=False
        self.available=self.windows_available or self.local_available
        for item in self.data.glob('*.wav'):
            if time.time()-item.stat().st_mtime>3600:item.unlink(missing_ok=True)
    def voices(self):
        if not self.speaker.is_file():return []
        result=subprocess.run([str(self.speaker),'--list'],capture_output=True,timeout=10,**hidden())
        if result.returncode:return []
        return json.loads(result.stdout.decode('utf-8-sig'))
    def status(self,preference='auto'):
        if preference not in ('auto','windows','local'):raise ValueError('無效的朗讀語音選項')
        selected=('windows' if self.windows_available else 'local') if preference=='auto' else preference
        available=self.windows_available if selected=='windows' else self.local_available
        label='Windows 繁中語音' if selected=='windows' else '隨包中文語音（MeloTTS）'
        return {'available':available,'backend':selected,'label':label,
                'windows_available':self.windows_available,'local_available':self.local_available,
                'local_language':'zh-CN','local_supports_traditional_text':True}
    def clear_cache(self):
        self.stop()
        with self.lock:
            for pattern in ('*.wav','*.txt'):
                for item in self.data.glob(pattern):item.unlink(missing_ok=True)
    def stop(self):
        with self.process_lock:
            self.cancel_epoch+=1;process=self.process
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
    def _start(self,command,epoch):
        # Register the owned child atomically with cancellation. Stopping during
        # text/audio preparation must also prevent a later child from starting.
        with self.process_lock:
            if epoch!=self.cancel_epoch:raise RuntimeError('語音工作已停止')
            self.process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**hidden())
    def recognize(self,audio):
        with self.process_lock:epoch=self.cancel_epoch
        if not self.model.is_file() or not self.recognizer.is_file():raise RuntimeError('本地語音辨識尚未準備好')
        try:
            with wave.open(io.BytesIO(audio),'rb') as wav:
                if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getframerate()!=16000 or wav.getcomptype()!='NONE':raise ValueError('錄音格式需要單聲道 16 kHz PCM')
                seconds=wav.getnframes()/16000
                if not .15<=seconds<=31:raise ValueError('請錄製 0.2～30 秒的語音')
                samples=array.array('h',wav.readframes(wav.getnframes()))
                if not samples or math.sqrt(sum(x*x for x in samples)/len(samples))<50:raise ValueError('沒有收到清楚的聲音，請再試一次')
        except (wave.Error,EOFError):raise ValueError('無法讀取錄音')
        if not self.lock.acquire(blocking=False):raise RuntimeError('語音工作尚在處理中')
        identifier=uuid.uuid4().hex;path=self.data/(identifier+'.wav');output=self.data/(identifier+'-asr')
        try:
            path.write_bytes(audio)
            self._start([str(self.recognizer),'--model',str(self.model),'--wav',str(path),'--out',str(output.with_suffix('.txt'))],epoch)
            self.process.wait(timeout=90)
            if self.process.returncode:raise RuntimeError('本地辨識已停止或未能完成')
            text=traditional(output.with_suffix('.txt').read_text(encoding='utf-8-sig')).strip()
            if not text:raise ValueError('沒有辨識到清楚語音')
            return text[:1200]
        except subprocess.TimeoutExpired:self.stop();raise RuntimeError('語音辨識超時，請縮短錄音')
        finally:
            self.process=None
            for item in [path,output.with_suffix('.txt')]:item.unlink(missing_ok=True)
            self.lock.release()
    def synthesize(self,text,rate=0,backend='auto'):
        with self.process_lock:epoch=self.cancel_epoch
        selected=self.status(backend)
        if not selected['available']:raise RuntimeError(selected['label']+'尚未準備好，請改選可用語音')
        speaker=self.speaker if selected['backend']=='windows' else self.local_speaker
        if not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>262144:raise ValueError('朗讀文字為空或超過容量')
        if type(rate)!=int or not -3<=rate<=3:raise ValueError('語速範圍為 -3～3')
        if not self.lock.acquire(timeout=3):raise RuntimeError('語音工作尚在處理中')
        identifier=uuid.uuid4().hex;text_path=self.data/(identifier+'.txt');wav_path=self.data/(identifier+'.wav');complete=False
        try:
            for item in self.data.glob('*.wav'):
                if time.time()-item.stat().st_mtime>3600:item.unlink(missing_ok=True)
            text_path.write_text(text,encoding='utf-8')
            self._start([str(speaker),'--text',str(text_path),'--wav',str(wav_path),'--rate',str(rate)],epoch)
            self.process.wait(timeout=120 if selected['backend']=='windows' else 630)
            if self.process.returncode or not wav_path.is_file():raise RuntimeError(selected['label']+'未能完成；未播放截斷音訊，請縮短文字或改選語音')
            with wave.open(str(wav_path),'rb') as wav:
                if wav.getcomptype()!='NONE' or wav.getsampwidth()!=2 or wav.getnchannels()!=1 or not 8000<=wav.getframerate()<=96000 or not wav.getnframes():raise RuntimeError('語音輸出格式無效')
                seconds=wav.getnframes()/wav.getframerate()
            complete=True
            return {'id':identifier,'seconds':seconds,'backend':selected['backend']}
        except subprocess.TimeoutExpired:self.stop();raise RuntimeError('語音合成超時')
        except Exception:
            wav_path.unlink(missing_ok=True)
            raise
        finally:
            self.process=None;text_path.unlink(missing_ok=True)
            if not complete:wav_path.unlink(missing_ok=True)
            self.lock.release()

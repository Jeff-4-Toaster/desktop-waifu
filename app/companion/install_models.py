"""Explicit, pinned model installation with progress; no user content is sent."""
import hashlib
import json
from pathlib import Path
import threading
import urllib.request

class Installer:
    def __init__(self,manifest,directory):
        self.manifest=json.loads(Path(manifest).read_text(encoding='utf-8'))
        self.directory=Path(directory);self.progress={'running':False,'bytes':0,'total':0,'error':''};self.lock=threading.Lock()
    def start(self,profile):
        if profile not in ('4B','8B'):raise ValueError('無效的模型配置')
        with self.lock:
            if self.progress['running']:raise ValueError('模型正在準備中')
            self.progress={'running':True,'bytes':0,'total':0,'error':''}
        threading.Thread(target=self._run,args=(profile,),daemon=True).start()
    def _run(self,profile):
        try:
            self.directory.mkdir(parents=True,exist_ok=True)
            entries=[self.manifest[profile],self.manifest['asr']]
            self.progress['total']=sum(x['size'] for x in entries)
            completed=0
            for item in entries:
                path=self.directory/item['name'];self.progress['file']=item['name']
                if path.exists():
                    with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    if digest==item['sha256']:completed+=item['size'];self.progress['bytes']=completed;continue
                part=path.with_name(path.name+'.part');offset=part.stat().st_size if part.exists() else 0
                while offset<item['size']:
                    end=min(item['size']-1,offset+8*1024*1024-1)
                    request=urllib.request.Request(item['url'],headers={'Range':f'bytes={offset}-{end}','User-Agent':'DesktopWaifu-local-model-setup'})
                    with urllib.request.urlopen(request,timeout=60) as response:
                        if response.status!=206 or not response.headers.get('Content-Range','').startswith(f'bytes {offset}-'):raise RuntimeError('下載來源未回傳正確區段，稍後可再次續傳')
                        block=response.read(end-offset+1)
                    if len(block)!=end-offset+1:raise RuntimeError('下載中斷，稍後可再次續傳')
                    with part.open('ab') as stream:stream.write(block)
                    offset+=len(block);self.progress['bytes']=completed+offset
                with part.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                if digest!=item['sha256']:raise RuntimeError('模型雜湊不符，請移除未完成的下載後再試')
                part.replace(path);completed+=item['size']
        except Exception as error:self.progress['error']=str(error)[:240]
        finally:self.progress['running']=False

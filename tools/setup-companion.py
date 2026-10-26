"""Download pinned, upstream local-AI dependencies. No admin install or cloud inference."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import time
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parent.parent
META=ROOT/'build/companion-setup'
CACHE=ROOT/'.tools/downloads/companion'
CACHE.mkdir(parents=True,exist_ok=True)
MODELS=ROOT/'models/companion'
MODELS.mkdir(parents=True,exist_ok=True)

def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        while block:=stream.read(4*1024*1024):digest.update(block)
    return digest.hexdigest()

def download(item):
    path=Path(item['destination']);path.parent.mkdir(parents=True,exist_ok=True)
    expected=item.get('sha256')
    if path.exists() and (not expected or sha(path)==expected):
        item['sha256']=sha(path);print('Verified existing',path.name,flush=True);return item
    part=path.with_name(path.name+'.part')
    for attempt in range(5):
        try:
            offset=part.stat().st_size if part.exists() else 0
            headers={'User-Agent':'DesktopWaifu-local-setup/1.0'}
            if offset:headers['Range']=f'bytes={offset}-'
            if item.get('size',0)>64*1024*1024:
                # Bounded ranges avoid stalled long response streams on CDN/proxy routes.
                while offset<item['size']:
                    end=min(item['size']-1,offset+8*1024*1024-1)
                    headers['Range']=f'bytes={offset}-{end}'
                    with urllib.request.urlopen(urllib.request.Request(item['url'],headers=headers),timeout=45) as response:
                        content_range=response.headers.get('Content-Range','')
                        if response.status!=206 or not content_range.startswith(f'bytes {offset}-'):raise ValueError('Server did not honor the download range')
                        data=response.read(end-offset+1)
                    if len(data)!=end-offset+1:raise ValueError('Incomplete range')
                    with part.open('ab') as stream:stream.write(data)
                    offset+=len(data)
                    if offset%(64*1024*1024)==0:print(path.name,round(offset/1024**2),'MiB',flush=True)
                actual=sha(part)
                if expected and actual!=expected:raise ValueError('SHA256 mismatch: '+path.name)
                part.replace(path);item['sha256']=actual;print('Downloaded and verified',path.name,path.stat().st_size,flush=True);return item
            with urllib.request.urlopen(urllib.request.Request(item['url'],headers=headers),timeout=90) as response:
                append=offset and response.status==206
                if not append:offset=0
                last=offset
                with part.open('ab' if append else 'wb') as stream:
                    while block:=response.read(4*1024*1024):
                        stream.write(block);offset+=len(block)
                        if offset-last>=256*1024*1024:
                            print(path.name,round(offset/1024**2),'MiB',flush=True);last=offset
            actual=sha(part)
            if expected and actual!=expected:raise ValueError('SHA256 mismatch: '+path.name)
            part.replace(path);item['sha256']=actual
            print('Downloaded and verified',path.name,path.stat().st_size,flush=True);return item
        except Exception as error:
            print('Retry',path.name,attempt+1,type(error).__name__,str(error)[:160],flush=True)
            if attempt==4:raise
            time.sleep(2+attempt)

def extract(path,target):
    target.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for entry in archive.infolist():
            if not (target/entry.filename).resolve().is_relative_to(target.resolve()):raise ValueError('Unsafe ZIP path')
        archive.extractall(target)

def main():
    lock=ROOT/'tools/companion-lock.json'
    pinned=json.loads(lock.read_text(encoding='utf-8'))
    items=pinned['downloads']
    for item in items:
        destination=(ROOT/item['path']).resolve()
        if not destination.is_relative_to(ROOT):raise ValueError('Unsafe dependency destination')
        item['destination']=str(destination)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(download,items))
    llama=pinned['llama'];whisper=pinned['whisper']
    extract(CACHE/f'llama-{llama}-bin-win-cuda-12.4-x64.zip',ROOT/'.tools'/('llama-'+llama))
    extract(CACHE/'cudart-llama-bin-win-cuda-12.4-x64.zip',ROOT/'.tools'/('llama-'+llama))
    extract(CACHE/'whisper-bin-x64.zip',ROOT/'.tools'/('whisper-'+whisper))
    extract(CACHE/'python-3.13.13-embed-amd64.zip',ROOT/'.tools/python-3.13.13-embed')
    extract(CACHE/'microsoft.web.webview2.1.0.4258.31.zip',ROOT/'.tools/webview2-sdk-1.0.4258.31')
    print('Pinned local companion dependencies ready',flush=True)

if __name__=='__main__':main()

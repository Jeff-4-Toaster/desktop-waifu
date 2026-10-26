"""Prepare a pinned CPU TTS dependency directory; network requires --download.

This tool never installs anything, starts a model, opens a user profile, or
changes the daily package. Runtime archives and non-LFS frontend Git objects
are verified before use. The prepared manifest records SHA256 for every file.
"""
import argparse
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
import urllib.request
import uuid
import zipfile

ROOT=Path(__file__).resolve().parent.parent
LOCK=Path(__file__).with_name('local-tts-lock.json')
DEFAULT=ROOT/'.tools/local-tts-melo-2.0.0'
MANIFEST='local-tts-manifest.json'
MAX_FILES=5000
MAX_FILE=256*1024*1024
MAX_ARCHIVE=512*1024*1024
FEATURE='local-tts-melo-20261025'


def sha(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def relative_name(name):
    if not isinstance(name,str) or not name or '\\' in name or ':' in name or name.startswith('/'):
        raise ValueError('Invalid relative path')
    parts=name.split('/')
    if any(not part or part in ('.','..') or part!=part.rstrip(' .') or
           re.search(r'[<>"|?*\x00-\x1f\x7f]',part) or
           re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',part) for part in parts):
        raise ValueError('Unsafe Windows path')
    return PurePosixPath(name)


def unique_names(names):
    seen=set()
    for name in names:
        relative_name(name)
        if name.casefold() in seen:raise ValueError('Case-aliased or duplicate path')
        seen.add(name.casefold())


def plain_path(value,boundary=ROOT):
    path=Path(value).absolute();boundary=Path(boundary).resolve()
    if path.drive.casefold()!=boundary.drive.casefold():raise ValueError('Path is outside the workspace')
    current=path
    while current!=boundary:
        if current.is_symlink() or (hasattr(current,'is_junction') and current.is_junction()):
            raise ValueError('Links and junctions are not supported')
        if current.parent==current:raise ValueError('Path is outside its allowed directory')
        current=current.parent
    resolved=path.resolve()
    if not resolved.is_relative_to(boundary):raise ValueError('Path escapes its allowed directory')
    return resolved


def destination_path(value):
    path=plain_path(value)
    if not any(path.is_relative_to((ROOT/name).resolve()) for name in ('.tools','build/companion-test')):
        raise ValueError('Preparation is limited to .tools or build/companion-test')
    return path


def allowed_host(host):
    return (host in ('github.com','raw.githubusercontent.com','huggingface.co') or
            any(host.endswith('.'+suffix) for suffix in ('githubusercontent.com','hf.co','huggingface.co','xethub.hf.co','amazonaws.com')))


def safe_url(value):
    parsed=urlsplit(value)
    if parsed.scheme!='https' or not allowed_host(parsed.hostname or '') or parsed.username or parsed.password:
        raise ValueError('Only official HTTPS sources and their download CDNs are allowed')
    return value


def load_lock(path=LOCK):
    path=plain_path(path)
    if path.stat().st_size>131072:raise ValueError('Lock file is too large')
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    if data.get('schema_version')!=1 or data.get('feature')!=FEATURE:raise ValueError('Unsupported TTS lock')
    runtime=data['runtime'];voice=data['voice']
    if runtime.get('version')!='2.0.0' or runtime.get('provider')!='cpu' or runtime.get('license')!='Apache-2.0':
        raise ValueError('Unsupported runtime')
    relative_name(runtime['archive_prefix'])
    if '/' in runtime['archive_prefix']:raise ValueError('Invalid archive prefix')
    if voice.get('repository')!='csukuangfj/vits-melo-tts-zh_en' or voice.get('license')!='MIT' or not re.fullmatch(r'[a-f0-9]{40}',voice.get('revision','')):
        raise ValueError('Unsupported voice identity')
    items=[runtime]+data['files']+data['notices']
    if len(items)>128:raise ValueError('Too many dependency files')
    for item in items:
        if type(item.get('size')) is not int or not 0<item['size']<=MAX_FILE:raise ValueError('Invalid dependency size')
        safe_url(item['url'])
        checksum=item['checksum'];kind=checksum.get('kind')
        width=64 if kind=='sha256' else 40 if kind=='git-blob-sha1' else 0
        if not width or not re.fullmatch('[a-f0-9]{'+str(width)+'}',checksum.get('value','')):
            raise ValueError('Invalid dependency checksum')
    if runtime['checksum']['kind']!='sha256':raise ValueError('Runtime must have a SHA256 pin')
    unique_names([item['path'] for item in data['files']+data['notices']])
    model=next(item for item in data['files'] if item['path']=='model/model.int8.onnx')
    if model['checksum']['kind']!='sha256':raise ValueError('ONNX weights must have a SHA256 pin')
    prefix='https://huggingface.co/'+voice['repository']+'/resolve/'+voice['revision']+'/'
    for item in data['files']:
        if not item['path'].startswith('model/') or item['url']!=prefix+item['path'][6:]:
            raise ValueError('Model/frontend URLs must use the pinned repository revision')
    if not data.get('license_coverage'):raise ValueError('License coverage is required')
    return data


class CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        safe_url(newurl)
        return super().redirect_request(request,fp,code,msg,headers,newurl)


def checksum_hasher(item):
    if item['checksum']['kind']=='sha256':return hashlib.sha256()
    digest=hashlib.sha1()
    digest.update(('blob '+str(item['size'])+'\0').encode('ascii'))
    return digest


def download(item,target,deadline,opener=None):
    target=plain_path(target);target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():raise FileExistsError('Dependency destination already exists')
    opener=opener or urllib.request.build_opener(CheckedRedirect())
    request=urllib.request.Request(safe_url(item['url']),headers={'User-Agent':'DesktopWaifu-local-TTS-preparation/1','Accept-Encoding':'identity'})
    expected=checksum_hasher(item);actual_sha=hashlib.sha256();length=0
    try:
        with opener.open(request,timeout=30) as response,target.open('xb') as output:
            safe_url(response.geturl())
            while True:
                if time.monotonic()>deadline:raise TimeoutError('Dependency preparation deadline exceeded')
                chunk=response.read(1024*1024)
                if not chunk:break
                length+=len(chunk)
                if length>item['size']:raise ValueError('Dependency exceeded its pinned length')
                expected.update(chunk);actual_sha.update(chunk);output.write(chunk)
        if length!=item['size'] or expected.hexdigest()!=item['checksum']['value']:
            raise ValueError('Dependency length or checksum mismatch')
        return {'size':length,'sha256':actual_sha.hexdigest(),'upstream_checksum':item['checksum']}
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def archive_members(entries,prefix):
    records=[];size=0
    if len(entries)>MAX_FILES:raise ValueError('Too many archive entries')
    for name,length,is_directory,is_plain in entries:
        name=name[:-1] if name.endswith('/') else name
        relative_name(name)
        if name.split('/')[0]!=prefix:raise ValueError('Unexpected runtime archive root')
        if not is_directory and not is_plain:raise ValueError('Archive links/devices are not supported')
        if length<0 or length>MAX_FILE:raise ValueError('Runtime member is too large')
        size+=length
        if size>MAX_ARCHIVE:raise ValueError('Runtime archive expands beyond its limit')
        records.append((name,length,is_directory))
    unique_names([name for name,_,_ in records])
    kinds={name:directory for name,_,directory in records}
    for name,_,_ in records:
        for parent in PurePosixPath(name).parents:
            if str(parent)!='.' and str(parent) in kinds and not kinds[str(parent)]:
                raise ValueError('Archive file is also an ancestor directory')
    return records


def copy_member(incoming,target,length):
    target.parent.mkdir(parents=True,exist_ok=True)
    size=0
    with target.open('xb') as output:
        while True:
            chunk=incoming.read(min(1024*1024,length-size+1))
            if not chunk:break
            size+=len(chunk)
            if size>length:raise ValueError('Archive member exceeds declared size')
            output.write(chunk)
    if size!=length:raise ValueError('Archive member is truncated')


def extract_archive(archive,directory,prefix):
    directory=plain_path(directory);directory.mkdir(parents=True,exist_ok=False)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zipped:
            entries=zipped.infolist()
            for entry in entries:
                if entry.flag_bits&1:raise ValueError('Encrypted archive member')
            records=archive_members([(e.filename,e.file_size,e.is_dir(),
                stat.S_IFMT(e.external_attr>>16) in (0,stat.S_IFREG)) for e in entries],prefix)
            for entry,(name,length,directory_flag) in zip(entries,records):
                target=plain_path(directory/name,directory)
                if directory_flag:target.mkdir(parents=True,exist_ok=True)
                else:
                    with zipped.open(entry) as incoming:copy_member(incoming,target,length)
    else:
        with tarfile.open(archive,'r:*') as tar:
            entries=tar.getmembers()
            records=archive_members([(e.name,e.size,e.isdir(),e.isfile() and not e.issparse()) for e in entries],prefix)
            for entry,(name,length,directory_flag) in zip(entries,records):
                target=plain_path(directory/name,directory)
                if directory_flag:target.mkdir(parents=True,exist_ok=True)
                else:
                    with tar.extractfile(entry) as incoming:copy_member(incoming,target,length)


@contextmanager
def preparation_lock(target):
    lock=target.with_name(target.name+'.prepare.lock')
    with lock.open('x',encoding='ascii') as stream:stream.write(str(os.getpid()))
    try:yield
    finally:lock.unlink()


def file_manifest(directory):
    result={}
    for path in sorted(directory.rglob('*')):
        plain_path(path,directory)
        if path.is_file() and path!=directory/MANIFEST:
            name=path.relative_to(directory).as_posix();relative_name(name)
            result[name]={'size':path.stat().st_size,'sha256':sha(path)}
    unique_names(result)
    return result


def verify(directory):
    directory=destination_path(directory)
    manifest=json.loads((directory/MANIFEST).read_text(encoding='utf-8'))
    if manifest.get('feature')!=FEATURE or manifest.get('schema_version')!=1:raise ValueError('Unsupported prepared manifest')
    if manifest.get('lock_sha256')!=sha(LOCK):raise ValueError('Preparation uses a different lock revision')
    expected=manifest.get('files')
    if not isinstance(expected,dict):raise ValueError('Invalid prepared manifest files')
    unique_names(expected)
    if file_manifest(directory)!=expected:raise ValueError('Prepared file tree or SHA256 mismatch')
    relative_name(manifest['runtime_root'])
    if not (directory/manifest['runtime_root']/'include/sherpa-onnx/c-api/c-api.h').is_file():
        raise ValueError('Missing unchanged upstream C API header')
    return manifest


def prepare(data,destination):
    destination=destination_path(destination)
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():raise FileExistsError('Prepared directory already exists; use --verify or select a new directory')
    stage=destination.with_name(destination.name+'.stage-'+uuid.uuid4().hex[:10])
    with preparation_lock(destination):
        stage.mkdir(exist_ok=False);deadline=time.monotonic()+900
        runtime=data['runtime'];archive=stage/'downloads/runtime.tar.bz2'
        try:
            download(runtime,archive,deadline)
            extract_archive(archive,stage/'runtime',runtime['archive_prefix'])
            for item in data['files']+data['notices']:
                download(item,stage/item['path'],deadline)
            runtime_root='runtime/'+runtime['archive_prefix']
            if not (stage/runtime_root/'include/sherpa-onnx/c-api/c-api.h').is_file():
                raise ValueError('Runtime does not contain the expected upstream C API header')
            dlls=[path for path in (stage/runtime_root).rglob('*.dll')]
            if not any(path.name.casefold()=='sherpa-onnx-c-api.dll' for path in dlls):
                raise ValueError('Runtime C API DLL is missing')
            # Some official layouts repeat an identical dependency in bin/lib.
            # Flattening is safe only when every spelling has the same bytes.
            flattened={}
            for path in sorted(dlls):
                key=path.name.casefold();digest=sha(path)
                if key in flattened and flattened[key]['sha256']!=digest:
                    raise ValueError('Runtime contains ambiguous DLL basenames')
                flattened.setdefault(key,{'path':path.relative_to(stage).as_posix(),'sha256':digest})
            manifest={'schema_version':1,'feature':FEATURE,'lock_sha256':sha(LOCK),'runtime_version':runtime['version'],
                      'runtime_root':runtime_root,'voice':data['voice'],'license_coverage':data['license_coverage'],
                      'network_explicitly_requested':True,'installed_to_system':False,
                      'runtime_dlls':flattened,'files':file_manifest(stage)}
            with (stage/MANIFEST).open('x',encoding='utf-8') as output:json.dump(manifest,output,ensure_ascii=False,indent=2);output.write('\n')
            # Same-parent rename is atomic on the intended Windows filesystem;
            # a destination that appeared concurrently is never overwritten.
            if destination.exists():raise FileExistsError('Preparation destination appeared during download')
            stage.rename(destination)
            return manifest
        except BaseException:
            # Keep this owned partial directory for diagnosis/retry review.
            # Never recursively delete an inferred path or replace old artifacts.
            raise


def self_test():
    base=ROOT/'build/companion-test'/('local-tts-prep-fixtures-'+uuid.uuid4().hex[:8]);base.mkdir(parents=True)
    checks={}
    for name in ('../outside','/absolute','C:/drive','folder\\file','trailing.','aux.txt','a//b','a/./b'):
        try:relative_name(name);raise AssertionError('Unsafe name accepted')
        except ValueError:pass
    checks['windows_path_traversal_and_device_names']=True
    try:unique_names(['same/file','SAME/FILE']);raise AssertionError('Alias accepted')
    except ValueError:pass
    checks['casefold_aliases']=True
    for label,entries in [('traversal',[('../outside',1,False,True)]),('link',[('root/link',0,False,False)]),
                          ('prefix',[('other/file',1,False,True)]),('large',[('root/file',MAX_FILE+1,False,True)]),
                          ('ancestor',[('root/a',1,False,True),('root/a/b',1,False,True)])]:
        try:archive_members(entries,'root');raise AssertionError('Bad archive accepted')
        except ValueError:pass
        checks['archive_'+label]=True
    archive=base/'good.zip'
    with zipfile.ZipFile(archive,'w') as zipped:zipped.writestr('root/中文 file.txt','fixture')
    extract_archive(archive,base/'extracted','root')
    assert (base/'extracted/root/中文 file.txt').read_text()=='fixture'
    checks['actual_zip_unicode_extraction']=True
    payload=b'fixture payload';item={'url':'https://huggingface.co/fixture','size':len(payload),
                                     'checksum':{'kind':'git-blob-sha1','value':hashlib.sha1(b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest()}}
    class Response(io.BytesIO):
        def geturl(self):return 'https://huggingface.co/fixture'
    class Opener:
        def open(self,*args,**kwargs):return Response(payload)
    observation=download(item,base/'fixture.bin',time.monotonic()+5,Opener())
    assert observation['sha256']==hashlib.sha256(payload).hexdigest()
    checks['git_blob_verification_records_sha256']=True
    broken={**item,'checksum':{'kind':'sha256','value':'0'*64}}
    try:download(broken,base/'broken.bin',time.monotonic()+5,Opener());raise AssertionError('Hash mismatch accepted')
    except ValueError:pass
    assert not (base/'broken.bin').exists();checks['bad_download_removed']=True
    try:download(item,base/'fixture.bin',time.monotonic()+5,Opener());raise AssertionError('Existing file overwritten')
    except FileExistsError:pass
    checks['no_overwrite']=True
    with preparation_lock(base/'prepared'):
        try:
            with preparation_lock(base/'prepared'):raise AssertionError('Concurrent lock accepted')
        except FileExistsError:pass
    checks['exclusive_prepare_lock']=True
    report=base/'report.json';report.write_text(json.dumps({'passed':True,'checks':checks,'network_used':False},indent=2)+'\n',encoding='utf-8')
    print(str(report));return 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--download',action='store_true')
    group.add_argument('--verify',type=Path)
    group.add_argument('--self-test',action='store_true')
    parser.add_argument('--destination',type=Path,default=DEFAULT)
    args=parser.parse_args(argv)
    if args.self_test:return self_test()
    data=load_lock()
    if args.verify:
        checked=verify(args.verify);print(json.dumps({'passed':True,'files':len(checked['files']),'network_used':False}));return 0
    if not args.download:
        print(json.dumps({'feature':FEATURE,'network_used':False,'download_required':True,
                          'download_bytes':data['runtime']['size']+sum(item['size'] for item in data['files']+data['notices']),
                          'destination':str(destination_path(args.destination)),
                          'checksum_kinds':sorted({item['checksum']['kind'] for item in [data['runtime']]+data['files']+data['notices']})},ensure_ascii=False,indent=2));return 0
    checked=prepare(data,args.destination)
    print(json.dumps({'passed':True,'prepared':str(args.destination),'files':len(checked['files']),
                      'manifest_sha256':sha(Path(args.destination)/MANIFEST),'installed_to_system':False},ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())

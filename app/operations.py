"""Admin-only live component health and backup freshness."""
import os,time,json,re,secrets
from threading import Lock,Thread
from pathlib import Path
from datetime import datetime,timezone
from concurrent.futures import ThreadPoolExecutor
import httpx,redis
from fastapi import Depends,HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy import select,func
from app.models import User,SessionRun,Audit,AccountState
from app.telephony import ami_action,voice_url
started=time.monotonic()

class ProcessCpuMeter:
    """One-second process CPU samples; 100% means one fully occupied core."""
    def __init__(self):
        self.lock=Lock()
        self.previous=None
        self.percent=None

    def sample(self,wall,cpu):
        with self.lock:
            if self.previous is not None:
                previous_wall,previous_cpu=self.previous
                elapsed=wall-previous_wall
                if elapsed>0:self.percent=round(max(0,(cpu-previous_cpu)/elapsed*100),1)
            self.previous=(wall,cpu)

    def run(self):
        while True:
            self.sample(time.monotonic(),time.process_time())
            time.sleep(1)

cpu_meter=ProcessCpuMeter()
Thread(target=cpu_meter.run,daemon=True,name='process-cpu-meter').start()

def process_metrics():
    try:
        resident_pages=int(Path('/proc/self/statm').read_text().split()[1])
        ram_mb=round(resident_pages*os.sysconf('SC_PAGE_SIZE')/1048576,1)
    except (OSError,ValueError,IndexError):ram_mb=None
    with cpu_meter.lock:cpu_percent=cpu_meter.percent
    return {'process_ram_mb':ram_mb,'process_cpu_percent':cpu_percent}

def backup_snapshot(root=None):
    root=Path(root or os.getenv('BACKUP_ROOT','/backups'))
    def read_json(name):
        try:return json.loads((root/name).read_text())
        except (OSError,ValueError):return {}
    def age(name):
        try:return max(0,round(time.time()-(root/name).stat().st_mtime))
        except OSError:return None
    meta=read_json('latest.json')
    bundle=meta.get('bundle','')
    valid=isinstance(bundle,str) and bundle.startswith('arm112_') and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_' for c in bundle)
    full=valid and all((root/bundle/name).is_file() for name in ('database.dump','media.tar.gz','runtime.tar.gz','SHA256SUMS','manifest.json'))
    elapsed=age('latest.dump')
    running=(root/'.backup-running').exists()
    queued=(root/'.backup-pending').is_dir()
    heartbeat=age('.scheduler-heartbeat')
    try:size=meta.get('size_bytes') if full else (root/'latest.dump').stat().st_size
    except OSError:size=None
    return {'status':'missing' if elapsed is None else 'stale' if elapsed>=86400 else 'incomplete' if not full else 'ok',
            'age_seconds':elapsed,'size_bytes':size,'scope':['database','media','runtime'] if full else ['database'],
            'created_at':meta.get('created_at'),'running':running,'queued':queued,
            'scheduler_alive':running or (heartbeat is not None and heartbeat<30) or (age('.restore-worker-heartbeat') is not None and age('.restore-worker-heartbeat')<30),
            'last_failed':(root/'last_error').exists(),'restore_check':read_json('last_restore_check.json'),
            'bundle':bundle if valid else None,'interval_hours':int(os.getenv('BACKUP_INTERVAL_HOURS','23'))}

def backup_catalog(root=None):
    root=Path(root or os.getenv('BACKUP_ROOT','/backups')).resolve()
    entries=[]
    try:children=list(root.iterdir())
    except OSError:return entries
    for child in children:
        if child.is_symlink() or not re.fullmatch(r'arm112_[A-Za-z0-9_]+(?:\.dump)?',child.name):continue
        try:
            if child.is_file() and child.suffix=='.dump':
                entries.append({'id':child.name,'created_at':datetime.fromtimestamp(child.stat().st_mtime,timezone.utc).isoformat(),'size_bytes':child.stat().st_size,'database_size_bytes':child.stat().st_size,'scope':['database'],'complete':True})
            elif child.is_dir():
                meta=json.loads((child/'manifest.json').read_text())
                created=datetime.fromisoformat(meta['created_at'].replace('Z','+00:00')).astimezone(timezone.utc).isoformat()
                names=('database.dump','media.tar.gz','runtime.tar.gz','SHA256SUMS','manifest.json')
                complete=all((child/name).is_file() and not (child/name).is_symlink() and (child/name).stat().st_size>0 for name in names)
                database=child/'database.dump'
                entries.append({'id':child.name,'created_at':created,'size_bytes':sum((child/name).stat().st_size for name in names[:3] if (child/name).is_file()),'database_size_bytes':database.stat().st_size if database.is_file() else None,'scope':['database','media','runtime'],'complete':complete})
        except (OSError,ValueError,TypeError,KeyError,AttributeError):continue
    return sorted(entries,key=lambda entry:entry['created_at'],reverse=True)

def register_operations(app,db,current):
    @app.get('/api/operations')
    def operations(u=Depends(current),s=Depends(db)):
        if u.role!='admin':raise HTTPException(403)
        def probe(name):
            try:
                if name=='redis':redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379'),socket_connect_timeout=1,socket_timeout=1).ping()
                elif name=='asterisk':ami_action('Ping')
                else:
                    urls={'ml':os.getenv('ML_URL','http://ml:8091')+'/health','voice':voice_url()+'/health','ollama':os.getenv('OLLAMA_URL','http://ollama:11434')+'/api/tags','grammar':os.getenv('GRAMMAR_URL','http://grammar:8093')+'/v2/check?language=ru-RU&text=Тест'}
                    with httpx.Client(timeout=1,trust_env=False) as c:c.get(urls[name]).raise_for_status()
                return name,{'status':'ok'}
            except Exception as exc:return name,{'status':'unavailable','reason':type(exc).__name__}
        with ThreadPoolExecutor(max_workers=6) as executor:components=dict(executor.map(probe,['redis','asterisk','ml','voice','grammar','ollama']))
        components['postgresql']={'status':'ok'};components['backup']=backup_snapshot()
        return {'at':datetime.now(timezone.utc),'uptime_seconds':round(time.monotonic()-started),**process_metrics(),'users':s.scalar(select(func.count(User.id))),'active_runs':s.scalar(select(func.count(SessionRun.id)).where(SessionRun.finished_at.is_(None))),'audit_events':s.scalar(select(func.count(Audit.id))),'components':components}

    @app.post('/api/operations/backup',status_code=202)
    def request_backup(u=Depends(current),s=Depends(db)):
        if u.role!='admin':raise HTTPException(403)
        from app.restoration import maintenance
        if maintenance():raise HTTPException(409,'Восстанавливается база данных')
        snapshot=backup_snapshot()
        if snapshot['running'] or snapshot['queued']:raise HTTPException(409,'Резервная копия уже создаётся или ожидает запуска')
        if not snapshot['scheduler_alive']:raise HTTPException(503,'Сервис резервирования недоступен. Проверьте контейнер backup')
        root=Path(os.getenv('BACKUP_ROOT','/backups'))
        try:(root/'.backup-pending').mkdir(mode=0o700)
        except FileExistsError:raise HTTPException(409,'Резервная копия уже ожидает запуска')
        s.add(Audit(user_id=u.id,action='backup.request',details={'scope':['database','media','runtime']}))
        s.commit()
        return {'status':'queued'}

    @app.get('/api/operations/backups')
    def list_backups(u=Depends(current)):
        if u.role!='admin':raise HTTPException(403)
        return backup_catalog()

    @app.get('/api/operations/backups/{backup_id}/database')
    def download_database(backup_id:str,u=Depends(current),s=Depends(db)):
        if u.role!='admin':raise HTTPException(403)
        item=next((entry for entry in backup_catalog() if entry['id']==backup_id),None)
        if item is None or not item['complete']:raise HTTPException(404,'Готовая резервная копия не найдена')
        root=Path(os.getenv('BACKUP_ROOT','/backups')).resolve()
        path=root/backup_id if backup_id.endswith('.dump') else root/backup_id/'database.dump'
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):raise HTTPException(404)
        s.add(Audit(user_id=u.id,action='backup.download',details={'backup_id':backup_id,'scope':'database'}));s.commit()
        return FileResponse(path,media_type='application/octet-stream',filename=backup_id if backup_id.endswith('.dump') else backup_id+'.dump',headers={'Cache-Control':'no-store'})

    @app.post('/api/operations/backups/{backup_id}/export',status_code=201)
    def export_full_backup(backup_id:str,u=Depends(current),s=Depends(db)):
        if u.role!='admin':raise HTTPException(403)
        from app.backup_archives import pack_bundle,ArchiveError
        from app.restoration import atomic_json,auth_epoch
        item=next((entry for entry in backup_catalog() if entry['id']==backup_id),None)
        if item is None or not item['complete'] or 'media' not in item['scope']:raise HTTPException(404,'Полная резервная копия не найдена')
        root=Path(os.getenv('BACKUP_ROOT','/backups'))
        exports=root/'exports';exports.mkdir(parents=True,exist_ok=True,mode=0o700)
        # Exports that were never downloaded expire after ten minutes.
        for old in exports.iterdir():
            if old.is_file() and time.time()-old.stat().st_mtime>600:old.unlink(missing_ok=True)
        token=secrets.token_hex(32);path=exports/(token+'.tar.gz')
        try:
            pack_bundle(root/backup_id,path);path.chmod(0o600)
            atomic_json(exports/(token+'.json'),{'user_id':u.id,'created_at':time.time(),'epoch':auth_epoch(),'filename':backup_id+'.tar.gz'})
        except (ArchiveError,OSError) as exc:
            path.unlink(missing_ok=True)
            raise HTTPException(422,str(exc))
        return {'url':'/api/operations/exports/'+token,'filename':backup_id+'.tar.gz','size_bytes':path.stat().st_size}

    @app.get('/api/operations/exports/{token}')
    def download_full_backup(token:str,s=Depends(db)):
        from app.restoration import auth_epoch
        if not re.fullmatch('[a-f0-9]{64}',token):raise HTTPException(404)
        exports=Path(os.getenv('BACKUP_ROOT','/backups'))/'exports'
        ticket=exports/(token+'.json');consumed=exports/(token+'.used')
        try:ticket.rename(consumed)
        except OSError:raise HTTPException(404,'Ссылка скачивания недействительна или уже использована')
        try:
            info=json.loads(consumed.read_text())
            user=s.get(User,info['user_id']);account=s.get(AccountState,info['user_id'])
            if time.time()-info['created_at']>600 or info['epoch']!=auth_epoch() or not user or user.role!='admin' or (account and account.blocked):raise HTTPException(403)
            path=exports/(token+'.tar.gz')
            if not path.is_file() or path.is_symlink():raise HTTPException(404)
            s.add(Audit(user_id=user.id,action='backup.download',details={'backup_id':info['filename'],'scope':'full'}));s.commit()
            return FileResponse(path,media_type='application/gzip',filename=info['filename'],headers={'Cache-Control':'no-store'},background=BackgroundTask(path.unlink,missing_ok=True))
        finally:consumed.unlink(missing_ok=True)

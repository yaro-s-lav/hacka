"""Actual Asterisk AMI calls and offline synthesized audio, tied to student sessions."""
import os, socket, secrets, threading, time, hmac, hashlib, base64
from pathlib import Path
from datetime import datetime,timezone
import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from app.models import User, SipAccount, VoipCall, SessionRun, RunContext, AccountState, Audit, CardEvent, Scenario
from app.workflows import get_run, ensure_writable, check_scenario_access, settings_for, aware
from app.speech_text import tts_text
provision_lock=threading.Lock()

def voice_url():
    configured=os.getenv('VOICE_URL','').strip()
    if configured:return configured.rstrip('/')
    return 'http://voice-silero:8092' if os.getenv('VOICE_ENGINE','piper').lower()=='silero' else 'http://voice:8092'

def frame(data):
    return ''.join(f'{key}: {value}\r\n' for key,value in data.items())+'\r\n'
def read_frame(stream):
    result={}
    while True:
        line=stream.readline()
        if not line:raise ConnectionError('AMI closed')
        line=line.decode().strip()
        if not line:
            if result:return result
            continue
        if ': ' in line:
            key,value=line.split(': ',1)
            if key=='Output':result.setdefault(key,[]).append(value)
            else:result[key]=value

def ami_connect():
    sock=socket.create_connection((os.getenv('ASTERISK_HOST','asterisk'),5038),timeout=5);stream=sock.makefile('rb');stream.readline()
    sock.sendall(frame({'Action':'Login','Username':'arm112','Secret':os.getenv('ASTERISK_AMI_SECRET','arm112-local-ami-change-me'),'Events':'on'}).encode())
    while True:
        reply=read_frame(stream)
        if reply.get('Response'):
            if reply['Response']!='Success':sock.close();raise ConnectionError('AMI login failed')
            return sock,stream

def ami_action(action,**fields):
    sock,stream=ami_connect()
    try:
        action_id=secrets.token_hex(12);sock.sendall(frame({'Action':action,'ActionID':action_id,**fields}).encode())
        while True:
            reply=read_frame(stream)
            if reply.get('ActionID')==action_id and reply.get('Response'):
                if reply['Response']=='Error':raise ConnectionError(reply.get('Message','AMI error'))
                return reply
    finally:stream.close();sock.close()

def hardware_password(account):
    # Stable, separately scoped credential; existing browser registration stays valid.
    alphabet='abcdefghjkmnpqrstuvwxyz23456789'
    digest=hmac.new(account.password.encode(),('arm112-hardware-v1:'+account.username).encode(),hashlib.sha256).digest()
    return ''.join(alphabet[int.from_bytes(digest[i*2:i*2+2],'big')%len(alphabet)] for i in range(10))

def write_accounts(s):
    blocked={x.user_id for x in s.scalars(select(AccountState).where(AccountState.blocked.is_(True)))}
    students=set(s.scalars(select(User.id).where(User.role=='student')))
    text='; Generated locally by ARM112; no external endpoints\n'
    for account in s.scalars(select(SipAccount).order_by(SipAccount.user_id)):
        if account.user_id in blocked or account.user_id not in students:continue
        for device in ('browser','hardware'):
            name=account.username+('-hw' if device=='hardware' else '')
            media = "webrtc=yes\nmedia_encryption=dtls\ndtls_auto_generate_cert=yes\nuse_avpf=yes\nice_support=yes\nrtcp_mux=yes" if device=='browser' else "webrtc=no\nmedia_encryption=no\nuse_avpf=no\nice_support=no\nrtcp_mux=no"
            # The hardware phone is behind Docker's SIP NAT. Force its SDP media
            # address to the server LAN address; browser/WebRTC must keep ICE.
            media_address = '' if device=='browser' else f"media_address={os.getenv('SIP_PUBLIC_ADDRESS', '').strip()}"
            text+=f"""[{name}]
type=endpoint
transport={'transport-ws' if device=='browser' else 'transport-udp'}
from_domain=arm112.local
context=training
disallow=all
allow={'ulaw,alaw' if device=='browser' else 'alaw,ulaw,g722,opus'}
auth={name}-auth
aors={name}
set_var=ARM_USER_ID={account.user_id}
{media}
{media_address}
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
direct_media=no
[{name}-auth]
type=auth
auth_type=userpass
username={name}
password={hardware_password(account) if device=='hardware' else account.password}
[{name}]
type=aor
max_contacts=1
remove_existing=yes
qualify_frequency=30
"""
    root=Path(os.getenv('SIP_PROVISION_ROOT','/provision'));root.mkdir(exist_ok=True)
    existing=root/'users.conf'
    if existing.is_file() and existing.read_text()==text:return
    temp=root/'users.conf.tmp';temp.write_text(text);temp.chmod(0o600);temp.replace(root/'users.conf')
    ami_action('Command',Command='pjsip reload')

def launch_worker(target,args):
    threading.Thread(target=target,args=args,daemon=True).start()

def prepare_speech(text,caller_name=''):
    with httpx.Client(timeout=httpx.Timeout(60,connect=5),trust_env=False) as client:
        response=client.post(voice_url()+'/speech',json={'text':tts_text(text),'caller_name':caller_name})
        response.raise_for_status()
        speech=response.json()
    sound=speech.get('key','')
    if len(sound)!=64 or any(c not in '0123456789abcdef' for c in sound):raise ValueError('Некорректный ответ сервиса озвучки')
    return speech

def phone_registered(account):
    reply=ami_action('Command',Command='pjsip show aor '+account)
    return any(line.lstrip().startswith('Contact:  '+account+'/') or
               (line.lstrip().startswith('Contact:') and line.split(':',1)[1].strip().startswith(account+'/'))
               for line in reply.get('Output',[]) if 'Unavail' not in line)

def call_error(reason):
    return {'0':'Телефон отклонил вызов или потерял регистрацию. Переподключите телефон и повторите.',
            '3':'Нет ответа от телефона. Проверьте соединение и разрешение микрофона.',
            '5':'Телефон занят другим разговором.',
            '8':'Телефон не принял вызов.'}.get(str(reason),'Не удалось соединить учебный вызов. Переподключите телефон и повторите.')

def register_telephony(app,db,current,session_factory):
    def permitted(s,u,run_id):
        run,context=get_run(s,u,run_id)
        return run,context
    def update(call_id,state,**values):
        with session_factory() as s:
            call=s.get(VoipCall,call_id)
            if not call or call.state=='cancelled':return
            call.state=state
            for key,value in values.items():setattr(call,key,value)
            run=s.get(SessionRun,call.run_id)
            s.add(CardEvent(run_id=run.id,user_id=run.student_id,kind='sip.'+state,data={'call_id':call.id,**{k:str(v) for k,v in values.items()}}))
            s.add(Audit(user_id=run.student_id,action='sip.'+state,details={'call_id':call.id,'run_id':run.id}))
            context=s.get(RunContext,run.id)
            s.commit()
    def cancelled(call_id):
        with session_factory() as s:
            call=s.get(VoipCall,call_id)
            return not call or call.state=='cancelled' or bool(s.get(SessionRun,call.run_id).finished_at)
    def worker(call_id,account,run_id,text,caller_name,aon):
        sock=None;stream=None
        try:
            if cancelled(call_id):return
            started=time.monotonic()
            speech=prepare_speech(text,caller_name);sound=speech['key']
            with session_factory() as session:
                if not cancelled(call_id):
                    session.add(CardEvent(run_id=run_id,user_id=session.get(SessionRun,run_id).student_id,kind='sip.audio_ready',data={'call_id':call_id,'voice':speech.get('voice'),'engine':speech.get('engine'),'cached':speech.get('cached',False),'preparation_ms':round((time.monotonic()-started)*1000)}));session.commit()
            if cancelled(call_id):return
            sock,stream=ami_connect();sock.settimeout(45)
            action_id=f'arm112-{call_id}'
            sock.sendall(frame({'Action':'Originate','ActionID':action_id,'Channel':'PJSIP/'+account,'Context':'training-playback','Exten':'s','Priority':1,'CallerID':f'Учебный абонент <{aon}>','Timeout':30000,'Async':'true','Variable':f'ARM_SOUND={sound},ARM_RUN_ID={run_id},ARM_CALL_ID={call_id}'}).encode())
            update(call_id,'ringing',sound_key=sound);channel=None;deadline=time.monotonic()+600
            while time.monotonic()<deadline:
                reply=read_frame(stream)
                if reply.get('ActionID')==action_id and reply.get('Response')=='Error':raise ConnectionError(reply.get('Message','Originate failed'))
                if reply.get('Event')=='OriginateResponse' and reply.get('ActionID')==action_id:
                    if reply.get('Response')!='Success':raise ConnectionError(call_error(reply.get('Reason','unknown')))
                    channel=reply.get('Channel')
                    if cancelled(call_id):
                        if channel:ami_action('Hangup',Channel=channel)
                        return
                    update(call_id,'answered',channel=channel,answered_at=datetime.now(timezone.utc));sock.settimeout(600)
                if reply.get('Event')=='Hangup' and channel and reply.get('Channel')==channel:
                    update(call_id,'ended',ended_at=datetime.now(timezone.utc));return
            raise TimeoutError('Call timeout')
        except httpx.TimeoutException:update(call_id,'failed',error='Подготовка озвучки заняла слишком много времени. Повторите звонок.',ended_at=datetime.now(timezone.utc))
        except httpx.HTTPError:update(call_id,'failed',error='Сервис озвучки недоступен. Повторите после восстановления сервиса.',ended_at=datetime.now(timezone.utc))
        except Exception as exc:update(call_id,'failed',error=str(exc) or 'Соединение с телефонией прервано. Повторите звонок.',ended_at=datetime.now(timezone.utc))
        finally:
            if stream:stream.close()
            if sock:sock.close()
    @app.get('/api/telephony/health')
    def health(u=Depends(current)):
        try:ami_action('Ping');return {'status':'ok','transport':'SIP/WebRTC','voice':'piper-russian'}
        except (OSError,ConnectionError):return {'status':'unavailable','message':'Профиль voip не запущен или Asterisk недоступен'}
    @app.post('/api/telephony/account')
    def account(request:Request,device:str='browser',u=Depends(current),s=Depends(db)):
        if device not in ('browser','hardware'):raise HTTPException(422,'Неизвестное устройство')
        if u.role!='student':raise HTTPException(403)
        try:
            with provision_lock:
                # Provision the cohort together so connecting another student
                # does not reload PJSIP under an already registered phone.
                known={x.user_id for x in s.scalars(select(SipAccount))}
                for user_id in s.scalars(select(User.id).where(User.role=='student')):
                    if user_id not in known:s.add(SipAccount(user_id=user_id,username='arm'+str(user_id),password=secrets.token_hex(24)))
                s.flush();account=s.get(SipAccount,u.id)
                write_accounts(s)
                s.add(Audit(user_id=u.id,action='sip.account',details={'username':account.username,'device':device,'recording_per_call':True}));s.commit()
        except (OSError,ConnectionError) as exc:raise HTTPException(503,'Asterisk недоступен') from exc
        turn_username=str(int(time.time())+3600)+':'+str(u.id)
        return {'username':account.username+('-hw' if device=='hardware' else ''),'device':device,'transport':'UDP' if device=='hardware' else 'WebSocket','codecs':['G.711A','G.711U','G.722','Opus'] if device=='hardware' else ['G.711U','G.711A'],'registrar':os.getenv('SIP_PUBLIC_ADDRESS',request.url.hostname) if device=='hardware' else request.url.hostname,'password':hardware_password(account) if device=='hardware' else account.password,'domain':request.url.hostname,'ws_path':'/sip-ws','ws_port':8088,'sip_port':5060,'ice_servers':[{'urls':'turn:'+request.url.hostname+':3478?transport=tcp','username':turn_username,'credential':base64.b64encode(hmac.new(os.getenv('TURN_SECRET','arm112-local-turn-change-me').encode(),turn_username.encode(),hashlib.sha1).digest()).decode()}]}
    @app.post('/api/telephony/runs/{run_id}/call')
    def call(run_id:int,device:str='browser',u=Depends(current),s=Depends(db)):
        if device not in ('browser','hardware'):raise HTTPException(422,'Неизвестное устройство')
        run,context=get_run(s,u,run_id,True)
        if u.role!='student' or run.student_id!=u.id:raise HTTPException(403)
        ensure_writable(run,context,s)
        if context and context.scenario_snapshot.get('mode')=='dispatch':raise HTTPException(409,'ДДС получает карточки; голосовой вызов относится к режиму 112')
        existing=s.scalar(select(VoipCall).where(VoipCall.run_id==run.id,VoipCall.state.in_(['queued','ringing','answered'])))
        if existing:return {'call_id':existing.id,'state':existing.state}
        account=s.get(SipAccount,u.id)
        if not account:raise HTTPException(409,'Сначала подключите учебный телефон')
        try:
            if not phone_registered(account.username+('-hw' if device=='hardware' else '')):
                raise HTTPException(409,'Аппаратный SIP-телефон не зарегистрирован. Проверьте линию на аппарате.' if device=='hardware' else 'Телефон не зарегистрирован. Нажмите «Подключить телефон» и повторите звонок.')
        except (OSError,ConnectionError) as exc:raise HTTPException(503,'Asterisk недоступен. Повторите после восстановления соединения.') from exc
        text=context.scenario_snapshot['caller_text']
        caller_name=context.scenario_snapshot.get('expected',{}).get('caller_name','') or ''
        # Keep this key in sync with voice-service cache versioning.
        run.answers={**(run.answers or {}),'channel':'SIP / IP-телефон'}
        new=VoipCall(run_id=run.id,sound_key='pending');s.add(new);s.flush()
        s.add(CardEvent(run_id=run.id,user_id=u.id,kind='sip.queued',data={'call_id':new.id,'device':device}));s.commit()
        launch_worker(worker,(new.id,account.username+('-hw' if device=='hardware' else ''),run.id,text,caller_name,''.join(filter(str.isdigit,context.scenario_snapshot.get('expected',{}).get('aon',''))) or '112'))
        return {'call_id':new.id,'state':new.state}
    @app.post('/api/telephony/scenarios/{scenario_id}/prepare')
    def prewarm(scenario_id:int,u=Depends(current),s=Depends(db)):
        if u.role!='student':raise HTTPException(403)
        scenario=s.get(Scenario,scenario_id)
        if not scenario:raise HTTPException(404)
        check_scenario_access(s,u,scenario)
        settings=settings_for(s,scenario)
        if settings and settings.mode=='dispatch':raise HTTPException(422,'Озвучка относится к карточке 112')
        text=scenario.caller_text;name=scenario.expected.get('caller_name','')
        s.rollback() # Do not hold a database connection during synthesis.
        started=time.monotonic()
        try:speech=prepare_speech(text,name)
        except (OSError,httpx.HTTPError,ValueError) as exc:raise HTTPException(503,'Не удалось заранее подготовить озвучку. Можно повторить звонок.') from exc
        return {'status':'ready','voice':speech.get('voice'),'cached':speech.get('cached',False),'preparation_ms':round((time.monotonic()-started)*1000)}

    @app.post('/api/telephony/runs/{run_id}/call/cancel')
    def cancel_call(run_id:int,u=Depends(current),s=Depends(db)):
        run,context=get_run(s,u,run_id,True)
        if u.role!='student' or run.student_id!=u.id:raise HTTPException(403)
        channels=[]
        for call in s.scalars(select(VoipCall).where(VoipCall.run_id==run.id,VoipCall.state.in_(['queued','ringing','answered']))):
            call.state='cancelled';call.ended_at=datetime.now(timezone.utc)
            if call.channel:channels.append(call.channel)
            s.add(CardEvent(run_id=run.id,user_id=u.id,kind='sip.cancelled',data={'call_id':call.id}))
        s.commit()
        for channel in channels:
            try:ami_action('Hangup',Channel=channel)
            except (OSError,ConnectionError):pass
        return {'status':'cancelled'}

    @app.post('/api/telephony/services/{extension}/prepare')
    def prepare_service(extension:str,u=Depends(current),s=Depends(db)):
        messages={'101':'Пожарная охрана. Диспетчер учебной службы. Передайте адрес, признаки пожара и сведения о пострадавших.',
                  '102':'Полиция. Учебный диспетчер слушает. Сообщите адрес и обстоятельства происшествия.',
                  '103':'Скорая помощь. Учебный диспетчер. Где находится пациент? Он в сознании? Дышит?',
                  '104':'Аварийная газовая служба. Учебный диспетчер. Сообщите адрес, где ощущается запах газа и есть ли угроза людям.'}
        if u.role!='student' or extension not in messages:raise HTTPException(403)
        try:
            with httpx.Client(timeout=60,trust_env=False) as client:
                response=client.post(voice_url()+'/speech',json={'text':messages[extension],'caller_name':'Диспетчер Алексей'});response.raise_for_status();key=response.json()['key']
            if not key or any(c not in '0123456789abcdef' for c in key):raise ValueError('Invalid sound key')
            ami_action('Command',Command=f'database put arm112 service{extension} {key}')
        except (OSError,ConnectionError,httpx.HTTPError,ValueError) as exc:raise HTTPException(503,'Учебная служба телефонии недоступна') from exc
        return {'extension':extension,'training':True}

    @app.post('/api/telephony/runs/{run_id}/transfer/{extension}')
    def transfer(run_id:int,extension:str,u=Depends(current),s=Depends(db)):
        run,context=permitted(s,u,run_id);ensure_writable(run,context,s)
        if u.role!='student' or run.student_id!=u.id:raise HTTPException(403)
        if extension not in ('101','102','103','104','900'):raise HTTPException(422,'Перевод разрешён только внутри учебного контура')
        active=s.scalar(select(VoipCall).where(VoipCall.run_id==run.id,VoipCall.state=='answered'))
        if not active or not active.channel:raise HTTPException(409,'Нет активного SIP-разговора')
        try:ami_action('Redirect',Channel=active.channel,Context='training',Exten=extension,Priority='1')
        except (OSError,ConnectionError) as exc:raise HTTPException(503,'Перевод вызова не выполнен') from exc
        s.add(CardEvent(run_id=run.id,user_id=u.id,kind='sip.transfer',data={'extension':extension,'call_id':active.id}));s.add(Audit(user_id=u.id,action='sip.transfer',details={'run_id':run.id,'extension':extension}));s.commit();return {'status':'transferred','extension':extension}

    @app.post('/api/telephony/runs/{run_id}/conference')
    def conference(run_id:int,u=Depends(current),s=Depends(db)):
        run,context=permitted(s,u,run_id);ensure_writable(run,context,s)
        if u.role!='student' or run.student_id!=u.id:raise HTTPException(403)
        active=s.scalar(select(VoipCall).where(VoipCall.run_id==run.id,VoipCall.state=='answered'))
        if not active or not active.channel:raise HTTPException(409,'Нет активного SIP-разговора')
        try:ami_action('Redirect',Channel=active.channel,Context='training',Exten='99'+str(u.id),Priority='1')
        except (OSError,ConnectionError) as exc:raise HTTPException(503,'Конференция не создана') from exc
        s.add(CardEvent(run_id=run.id,user_id=u.id,kind='sip.conference',data={'extension':'99'+str(u.id),'call_id':active.id}));s.commit();return {'extension':'99'+str(u.id),'status':'conference'}

    @app.get('/api/telephony/runs/{run_id}')
    def calls(run_id:int,u=Depends(current),s=Depends(db)):
        permitted(s,u,run_id)
        audio={e.data['call_id']:e.data for e in s.scalars(select(CardEvent).where(CardEvent.run_id==run_id,CardEvent.kind=='sip.audio_ready'))}
        return [{'id':x.id,'state':x.state,'created_at':aware(x.created_at),'answered_at':aware(x.answered_at),'ended_at':aware(x.ended_at),'error':x.error,'audio':audio.get(x.id)} for x in s.scalars(select(VoipCall).where(VoipCall.run_id==run_id).order_by(VoipCall.id))]
    @app.get('/api/telephony/runs/{run_id}/recording')
    def recording(run_id:int,call_id:int|None=None,u=Depends(current),s=Depends(db)):
        permitted(s,u,run_id)
        calls=list(s.scalars(select(VoipCall).where(VoipCall.run_id==run_id).order_by(VoipCall.id)))
        selected=next((c for c in calls if c.id==call_id),None) if call_id is not None else None
        if call_id is not None and not selected:raise HTTPException(404,'Звонок этой карточки не найден')
        active=[c for c in calls if c.state in ('queued','ringing','answered')]
        if (selected and selected in active) or (call_id is None and active):raise HTTPException(409,'Запись ещё идёт')
        from app.recordings import recording_files
        files=recording_files(s,run_id,calls)
        path=files.get(call_id) if call_id is not None else files[max(files)] if files else None
        if not path:raise HTTPException(404,'Запись ещё не создана')
        return FileResponse(path,media_type='audio/wav')

def recover_interrupted_calls(s):
    # Origination workers belong to this process. After restart their calls need
    # a terminal state, otherwise a retry would return a stale active call forever.
    for call in s.scalars(select(VoipCall).where(VoipCall.state.in_(['queued','ringing','answered']))):
        if call.channel:
            try:ami_action('Hangup',Channel=call.channel)
            except (OSError,ConnectionError):pass
        call.state='failed';call.error='Вызов прерван перезапуском сервиса';call.ended_at=datetime.now(timezone.utc)
        s.add(Audit(action='sip.recovered',details={'call_id':call.id,'run_id':call.run_id}))
    s.commit()

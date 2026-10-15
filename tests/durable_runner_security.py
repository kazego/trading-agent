"""Synthetic runner controls; no operational modules or filesystem side effects.

Ephemeral per-case capabilities are sent through stdin, never persisted/logged.
The immutable binding stores only their digest. This is not an admin-resistant
authorization system, nor a replacement for OS sandboxing.
"""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from urllib.parse import unquote, urlsplit

INDIVIDUAL_SECONDS = 180
GLOBAL_SECONDS = 1800
ACTIONS = {'init','apply','apply-next','recover','corrupt','restore-old','corrupt-continuity'}
FAULTS = {'','before_event','after_event','after_financial','after_state','after_commit',
          'after_publish','partial_record','record_synced','receipt_synced'}
NAME = re.compile(r'incremental-[0-9a-f]{32}\Z')


def encode_telemetry(record):
    """Worker diagnostics ONLY; never a financial/journal/continuity encoder.

    Floats are allowed only in the two timing fields. Nonfinite JSON numbers,
    financial fields and unexpected nested objects remain rejected.
    """
    fields={'command','case','action','fault','duration_s','status','exit','stdout',
            'stderr','error','timeout_s','child_reaped'}
    if not isinstance(record,dict) or set(record)-fields:
        raise ValueError('Unexpected telemetry field')
    for key,value in record.items():
        if key in ('duration_s','timeout_s'):
            if type(value) not in (int,float) or value<0:raise ValueError('Invalid telemetry duration')
        elif key=='command':
            if not isinstance(value,list) or any(type(part) is not str for part in value):
                raise ValueError('Invalid telemetry command')
        elif key=='exit':
            if value is not None and type(value) is not int:raise ValueError('Invalid exit code')
        elif key=='child_reaped':
            if type(value) is not bool:raise ValueError('Invalid child status')
        elif type(value) is not str:raise ValueError('Invalid telemetry text')
    return json.dumps(record,sort_keys=True,separators=(',',':'),ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def validate_grant(grant, binding):
    if not isinstance(grant,dict) or set(grant)!={'case','suite','token','action','fault'}:
        raise PermissionError('Invalid worker grant shape')
    if any(not isinstance(grant[k],str) for k in grant):raise PermissionError('Invalid grant type')
    if not NAME.fullmatch(grant['case']) or not NAME.fullmatch(grant['suite']):
        raise PermissionError('Invalid case/suite identity')
    if grant['action'] not in ACTIONS or grant['fault'] not in FAULTS:
        raise PermissionError('Invalid action/fault')
    if grant['fault'] and grant['action'] not in ('apply','recover'):
        raise PermissionError('Fault not permitted for action')
    if not re.fullmatch('[0-9a-f]{64}',grant['token']):raise PermissionError('Invalid capability')
    expected={'case':grant['case'],'suite':grant['suite'],
              'token_hash':hashlib.sha256(grant['token'].encode()).hexdigest()}
    if not isinstance(binding,dict) or set(binding)!=set(expected):raise PermissionError('Invalid binding')
    if not hmac.compare_digest(json.dumps(binding,sort_keys=True),json.dumps(expected,sort_keys=True)):
        raise PermissionError('Case not owned by this invocation')
    return grant


class Registry:
    def __init__(self,suite):
        self.suite=suite;self.cases={}

    def register_new(self,path):
        # Called only on return from exclusive new_run(), never a caller path.
        if path in self.cases:raise PermissionError('Already registered')
        token=secrets.token_hex(32)
        self.cases[path]=token
        return {'case':path.name,'suite':self.suite.name,
                'token_hash':hashlib.sha256(token.encode()).hexdigest()}

    def grant(self,path,action,fault):
        if path not in self.cases:raise PermissionError('Foreign case')
        value={'case':path.name,'suite':self.suite.name,'token':self.cases[path],
               'action':action,'fault':fault}
        binding={'case':path.name,'suite':self.suite.name,
                 'token_hash':hashlib.sha256(self.cases[path].encode()).hexdigest()}
        return validate_grant(value,binding)


def strict_target(raw):
    p=Path(raw).absolute()
    parent=p.parent.resolve(strict=True)
    if parent!=p.parent:raise PermissionError('Aliased parent')
    if p.exists():
        if p.resolve(strict=True)!=p or p.is_symlink() or p.is_junction():raise PermissionError('Aliased target')
        if p.is_file() and p.stat().st_nlink!=1:raise PermissionError('Hardlinked target')
    return p


class Policy:
    def __init__(self,data_roots,repo,library_roots,resolve=strict_target):
        self.data_roots=set(data_roots);self.repo=repo;self.library_roots=tuple(library_roots)
        self.resolve=resolve;self.launch=None;self.mkdir_target=None

    def inside(self,p):return any(p==r or p.is_relative_to(r) for r in self.data_roots)

    def source(self,p):
        # Fixed trusted source/runtime reads; never broad project data access.
        if any(p.is_relative_to(r) for r in self.library_roots):return p.suffix.lower() not in ('.sqlite','.db')
        return p.suffix.lower() in ('.py','.pyc') and (p.parent==self.repo or
            any(p.is_relative_to(self.repo/name) for name in ('paper','research','tests','backtest','forward')))

    def __call__(self,event,args):
        if event.startswith(('socket.','urllib.','os.exec','os.spawn')) or event in (
                'os.system','os.startfile','os.startfile/2','os.posix_spawn','os.posix_spawnp'):
            raise PermissionError('Network/alternate execution denied')
        if event=='subprocess.Popen':
            if self.launch is None:raise PermissionError('Worker cannot launch children')
            executable,command,cwd,env=args
            expected_exe,expected_command,expected_cwd=self.launch
            # CPython serializes argv before auditing on Windows. Compare the
            # exact serialization, never a prefix or loosely parsed command.
            if os.name=='nt':expected_command=subprocess.list2cmdline(expected_command)
            if executable!=expected_exe or command!=expected_command or cwd!=expected_cwd or env is not None:
                raise PermissionError('Manipulated child invocation')
        if event in ('os.remove','os.rmdir','os.rename','os.chmod','os.link','os.symlink'):
            raise PermissionError('Cleanup/replacement denied')
        if event=='os.mkdir':
            if self.mkdir_target is None or self.resolve(args[0])!=self.mkdir_target:
                raise PermissionError('Unassigned directory creation')
        if event in ('os.listdir','os.scandir'):
            p=self.resolve(args[0])
            if not (self.inside(p) or p==self.repo or any(p.is_relative_to(r) for r in self.library_roots)
                    or any(p==self.repo/n or p.is_relative_to(self.repo/n) for n in ('paper','research','tests','backtest','forward'))):
                raise PermissionError('Foreign directory read')
        if event=='sqlite3.connect':
            raw=str(args[0])
            if raw==':memory:':return
            if raw.startswith('file:'):
                u=urlsplit(raw)
                if u.netloc or u.query!='mode=rw' or u.fragment:raise PermissionError('SQLite URI denied')
                raw=unquote(u.path).lstrip('/')
            p=self.resolve(raw)
            if not self.inside(p) or p.name!='synthetic.sqlite':raise PermissionError('Foreign SQLite')
        if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
            p=self.resolve(os.fsdecode(args[0]));mode,flags=args[1:3]
            writing=(isinstance(mode,str) and any(c in mode for c in 'wax+')) or (
                isinstance(flags,int) and flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
            if not self.inside(p) and (writing or not self.source(p)):
                raise PermissionError('Access outside assigned case/trusted source')


def report_preserving(primary,write,label):
    """Report failures must not replace a pre-existing worker/test failure."""
    try:write()
    except BaseException as error:
        if primary is None:raise
        primary.add_note(f'{label} also failed: {type(error).__name__}: {error}')


def invoke(command,grant,policy,deadline,emit,*,expect=0,clock=time.monotonic,run=subprocess.run):
    started=clock();remaining=deadline-started
    record={'command':command,'case':grant['case'],'action':grant['action'],'fault':grant['fault'],
            'duration_s':0,'status':'FAIL','exit':None,'stdout':'','stderr':''}
    def text(value):return value.decode('utf-8',errors='replace') if isinstance(value,bytes) else value or ''
    failure=None
    try:
        if remaining<=0:raise TimeoutError('Global 1800-second limit exhausted before launch')
        policy.launch=(command[0],command.copy(),str(policy.repo))
        result=run(command,executable=command[0],input=json.dumps(grant),capture_output=True,text=True,
                   cwd=str(policy.repo),timeout=min(INDIVIDUAL_SECONDS,remaining))
        record.update(exit=result.returncode,stdout=result.stdout,stderr=result.stderr)
        if clock()>deadline:raise TimeoutError('Global limit reached')
        if result.returncode!=expect:
            error=RuntimeError('Unexpected worker exit')
            error.add_note(f'Exit {result.returncode}; stderr: {result.stderr}; stdout: {result.stdout}')
            raise error
        value=json.loads(result.stdout) if result.returncode==0 else None
        record['status']='PASS';return value
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills and waits for this direct child before raising.
        # Workers are forbidden to create grandchildren.
        record.update(error='TimeoutExpired',timeout_s=exc.timeout,
                      stdout=text(exc.stdout),stderr=text(exc.stderr),child_reaped=True)
        failure=RuntimeError('Worker timeout; matrix FAIL')
        failure.add_note(f'Timeout {exc.timeout}; stderr: {text(exc.stderr)}; stdout: {text(exc.stdout)}')
        raise failure from exc
    except BaseException as exc:
        failure=exc;record['error']=type(exc).__name__+': '+str(exc);raise
    finally:
        policy.launch=None
        def report():
            record['duration_s']=clock()-started;emit(record)
        report_preserving(failure,report,'Worker report')

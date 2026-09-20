"""Explicit arm/poll only; --help is offline. No background work on import."""
import argparse
from datetime import datetime, timezone
import re
import time

from forward.manifest import ROOT, freeze
from forward.model import LABEL
from research.spec import encoded


def main(argv=None):
    parser = argparse.ArgumentParser(description=LABEL+' closed-candle observer')
    commands = parser.add_subparsers(dest='command', required=True)
    arm = commands.add_parser('arm', help='LOCAL activation only; freezes future T0, no data fetching')
    arm.add_argument('--run-id', required=True)
    arm.add_argument('--t0', required=True, help='UTC ISO timestamp ending Z on a future 4h boundary')
    arm.add_argument('--ack-prospective-exposure', action='store_true', required=True)
    poll = commands.add_parser('poll', help='EXPLICIT public network access for an already armed run')
    poll.add_argument('--run-id', required=True)
    poll.add_argument('--public-data-only', action='store_true', required=True)
    for name in ('status','latest','summary'):
        read=commands.add_parser(name,help='Read-only local report; no network or new signals')
        read.add_argument('--run-id',required=True)
    for name in ('plan-operations','bind-operations'):
        op=commands.add_parser(name,help='Explicit operational migration; no polling')
        op.add_argument('--run-id',required=True)
        op.add_argument('--effective-boundary',required=True)
        if name=='bind-operations':
            op.add_argument('--approved-implementation',required=True)
            op.add_argument('--approved-plan',required=True)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', args.run_id):
            raise ValueError('Invalid run identifier')
        directory = ROOT/'artifacts'/'forward-paper'/args.run_id
        # Do not follow symlinks/junctions out of the dedicated output tree.
        if directory.resolve() != directory.absolute():
            raise ValueError('Redirected artifact path denied')
        if args.command == 'arm':
            if not args.t0.endswith('Z'): raise ValueError('Explicit UTC required')
            stamp = datetime.fromisoformat(args.t0.replace('Z','+00:00'))
            if stamp.tzinfo != timezone.utc: raise ValueError('UTC required')
            delta = stamp-datetime(1970,1,1,tzinfo=timezone.utc)
            if delta.microseconds: raise ValueError('Whole-second T0 required')
            t0 = (delta.days*86400+delta.seconds)*1000
            record = freeze(directory,t0=t0,created_ms=time.time_ns()//1_000_000)
            print(encoded(dict(label=LABEL,status='ARMED_LOCAL_ONLY',manifest=record)).decode())
        elif args.command in ('status','latest','summary'):
            from forward.readout import safe_report
            result=safe_report(directory,args.command,now_ms=time.time_ns()//1_000_000)
            print(encoded(result).decode())
            if result['health']=='INTEGRITY_OR_AUTHORIZATION_FAILURE': return 1
        elif args.command in ('plan-operations','bind-operations'):
            from forward.operations import plan,bind
            if not args.effective_boundary.endswith('Z'): raise ValueError('UTC required')
            stamp=datetime.fromisoformat(args.effective_boundary.replace('Z','+00:00'))
            delta=stamp-datetime(1970,1,1,tzinfo=timezone.utc)
            if delta.microseconds: raise ValueError('Whole-second boundary required')
            effective=(delta.days*86400+delta.seconds)*1000
            params=dict(effective_ms=effective,now_ms=time.time_ns()//1_000_000)
            if args.command=='plan-operations': result=plan(directory,**params)
            else: result=bind(directory,**params,approved_implementation=args.approved_implementation,approved_plan=args.approved_plan)
            print(encoded(result).decode())
        else:
            from forward.observer import poll as run_poll
            print(encoded(run_poll(directory)).decode())
        return 0
    except Exception:
        # Never echo remote payloads, arbitrary exceptions or environment values.
        print(LABEL+' FAIL — integrity/authorization check failed; no repair attempted')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

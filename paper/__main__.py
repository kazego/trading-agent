"""Explicit LIVE-0B commands. Imports never activate or consume."""
import argparse
from pathlib import Path
from paper.model import LABEL
from paper.operations import Authorization,activate,load
from paper.consumer import consume,utc_ms
from paper.readout import report
from research.spec import encoded


def main(argv=None):
    parser=argparse.ArgumentParser(description=LABEL)
    parser.add_argument('command',choices=('consume','verify','activate','status','latest','trades','missed','summary'))
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--reviewed-manifest')
    parser.add_argument('--backend',choices=('traditional','incremental'),default='traditional')
    args=parser.parse_args(argv)
    if args.command=='activate':
        if not args.reviewed_manifest: parser.error('Explicit reviewed manifest required')
        auth=Authorization(Path(args.reviewed_manifest).read_bytes().rstrip(b'\n'))
        if auth.record['run_id']!=args.run_id: raise ValueError('Run mismatch')
        activate(auth,now_ms=utc_ms(),clock=utc_ms)
        result=dict(label=LABEL,activation=auth.id)
    elif args.command=='verify':
        result=dict(label=LABEL,activation=load(args.run_id).id)
    elif args.command=='consume': result=consume(args.run_id,backend=args.backend)
    else: result=report(args.run_id,args.command,now_ms=utc_ms())
    print(encoded(result).decode())


if __name__=='__main__': main()

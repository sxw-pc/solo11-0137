import argparse
import json
from pathlib import Path
from .store import connect
from .source import read,record_file
from . import notice,waterway,resources,arrival,power,report

HANDLERS={'notice-import':notice.import_data,'waterway-import':waterway.import_data,
          'resource-plan':resources.import_data,'arrival-import':arrival.import_data,
          'power-import':power.import_data}

def main():
    parser=argparse.ArgumentParser(prog='portbook')
    parser.add_argument('command',choices=[*HANDLERS,'report','audit','demo'])
    parser.add_argument('--db',default='portbook.db')
    parser.add_argument('--file')
    args=parser.parse_args()
    db=connect(args.db)
    if args.command=='report':
        print(report.render(db)); return
    if args.command=='audit':
        print(json.dumps(report.audit(db),ensure_ascii=False,indent=2)); return
    if args.command=='demo':
        root=Path(__file__).resolve().parent.parent/'fixtures'
        outputs=[]
        for cmd,file in [('notice-import','notices-ok.json'),('waterway-import','waterway-ok.json'),('resource-plan','resources-ok.json'),('arrival-import','arrival-ok.json')]:
            data,_=read(root/file)
            result=HANDLERS[cmd](db,data)
            record_file(db,root/file,cmd)
            outputs.append({'command':cmd,'result':result})
        print(json.dumps({'steps':outputs,'report':report.build(db),'audit':report.audit(db)},ensure_ascii=False,default=str,indent=2));return
    if not args.file: parser.error('--file is required')
    data,_=read(args.file)
    result=HANDLERS[args.command](db,data)
    record_file(db,args.file,args.command)
    print(json.dumps(result,ensure_ascii=False,default=str,indent=2))

if __name__=='__main__': main()

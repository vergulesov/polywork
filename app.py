import argparse, os
from dotenv import load_dotenv
from collector_fl import collect
from classifier import classify
from notifier import send
from storage import init_db, is_seen, mark_seen

load_dotenv()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--seed',action='store_true')
    args=ap.parse_args()
    init_db()
    projects=collect()
    print('collected',len(projects))
    if args.seed:
        for p in projects:
            if not is_seen(p['source'],p['external_id']):
                mark_seen(p,'SEEDED')
        print('seed done')
        return
    notify_check=os.getenv('NOTIFY_CHECK','true').lower() in {'1','true','yes','on'}
    for p in projects:
        if is_seen(p['source'],p['external_id']):
            continue
        try:
            v=classify(p)
            mark_seen(p,v.get('status','SEEN'))
            if v.get('status')=='TOP' or (notify_check and v.get('status')=='Проверить'):
                send(p,v)
            print(p['external_id'],v.get('status'),v.get('effective_rub_per_h'))
        except Exception as e:
            print('project error',p['external_id'],type(e).__name__,e)

if __name__=='__main__':
    main()
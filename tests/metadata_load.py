"""Opt-in 50-request local metadata burst; execute directly with .venv Python."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from statistics import mean
import threading
import time

from conftest import API, ROOT


def main():
    clients=[API('editor'),API('viewer')]
    route='/api/v1/workspaces/00000000-0000-4000-8000-000000000010/items'
    try:
        for api in clients: assert api.client.get(route).status_code==200
        barrier=threading.Barrier(50)
        def request(i):
            barrier.wait(timeout=10)
            started=time.perf_counter()
            response=clients[i%2].client.get(route)
            return response.status_code,(time.perf_counter()-started)*1000
        with ThreadPoolExecutor(max_workers=50) as pool:
            results=list(pool.map(request,range(50)))
        times=sorted(duration for _,duration in results)
        report={'concurrentRequests':50,'accounts':2,'successes':sum(s==200 for s,_ in results),
                'p95Ms':round(times[47],2),'meanMs':round(mean(times),2),'maxMs':round(times[-1],2),
                'scope':'One local burst through proxy; not a sustained production SLO measurement.'}
        (ROOT/'.local/metadata-load.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report))
        assert report['successes']==50,report
        assert report['p95Ms']<=700,report
    finally:
        for api in clients:api.client.close()


if __name__=='__main__':main()

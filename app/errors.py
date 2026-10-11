from http import HTTPStatus
from fastapi.responses import JSONResponse

class Problem(Exception):
    def __init__(self, status, code, detail, **extras):
        self.status, self.code, self.detail, self.extras = status, code, detail, extras
        super().__init__(code)


def problem_response(request, exc):
    content = dict(type=f'urn:fileflow:problem:{exc.code.lower().replace("_", "-")}',
                   title=HTTPStatus(exc.status).phrase,status=exc.status,detail=exc.detail,
                   code=exc.code,requestId=str(request.state.request_id),
                   retryable=exc.status in (429,503),instance=request.url.path,**exc.extras)
    headers = {'Cache-Control':'no-store'}
    if exc.status in (429,503):
        headers['Retry-After']='30'
    return JSONResponse(content,status_code=exc.status,media_type='application/problem+json',headers=headers)

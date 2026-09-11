import uuid
from contextvars import ContextVar
from django.contrib.auth import logout

request_id = ContextVar('request_id', default='')

class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        request.request_id = str(uuid.uuid4())
        token = request_id.set(request.request_id)
        try:
            response = self.get_response(request)
            response['X-Request-ID'] = request.request_id
            response['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
            if request.path != '/healthz/':
                response['Cache-Control'] = 'no-store'
            return response
        finally:
            request_id.reset(token)

class ActiveAccountMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
    def __call__(self, request):
        if request.user.is_authenticated and not request.user.is_active:
            logout(request)
        return self.get_response(request)

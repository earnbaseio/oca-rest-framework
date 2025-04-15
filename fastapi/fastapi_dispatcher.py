# Copyright 2022 ACSONE SA/NV
# License LGPL-3.0 or later (http://www.gnu.org/licenses/LGPL).

from contextlib import contextmanager
from io import BytesIO

from odoo.http import Dispatcher, request

from .context import odoo_env_ctx
from .error_handlers import convert_exception_to_status_body


class FastApiDispatcher(Dispatcher):
    routing_type = "fastapi"

    @classmethod
    def is_compatible_with(cls, request):
        return True

    def dispatch(self, endpoint, args):
        # don't parse the httprequest let starlette parse the stream
        self.request.params = {}  # dict(self.request.get_http_params(), **args)
        environ = self._get_environ()
        full_path = environ["PATH_INFO"]
        
        # Xử lý đặc biệt cho đường dẫn /api/v1/public/*
        if full_path.startswith("/api/v1/public/"):
            root_path = "/api/v1"
        else:
            root_path = "/".join(full_path.split("/")[:-1])
        
        # Thêm log để debug
        import logging
        _logger = logging.getLogger(__name__)
        _logger.info(f"[FastAPI Debug] Dispatching request to: {full_path}")
        _logger.info(f"[FastAPI Debug] Root path: {root_path}")

        fastapi_endpoint = self.request.env["fastapi.endpoint"].sudo()
        
        # Log các endpoint đã đăng ký
        endpoints = fastapi_endpoint.search([])
        _logger.info(f"[FastAPI Debug] Registered endpoints: {len(endpoints)}")
        for ep in endpoints:
            _logger.info(f"[FastAPI Debug] Endpoint: ID={ep.id}, Name={ep.name}, Root Path={ep.root_path}, App={ep.app}")
        
        app = fastapi_endpoint.get_app(root_path)
        _logger.info(f"[FastAPI Debug] App for {root_path}: {app is not None}")
        
        uid = fastapi_endpoint.get_uid(root_path)
        _logger.info(f"[FastAPI Debug] UID for {root_path}: {uid}")
        
        if app is None:
            _logger.error(f"[FastAPI Debug] App is None for root_path: {root_path}")
            raise Exception(f"No FastAPI app found for path: {root_path}")
            
        data = BytesIO()
        with self._manage_odoo_env(uid):
            try:
                for r in app(environ, self._make_response):
                    data.write(r)
                if self.inner_exception:
                    _logger.error(f"[FastAPI Debug] Inner exception: {self.inner_exception}")
                    raise self.inner_exception
                return self.request.make_response(
                    data.getvalue(), headers=self.headers, status=self.status
                )
            except Exception as e:
                _logger.error(f"[FastAPI Debug] Exception in dispatch: {str(e)}")
                raise

    def handle_error(self, exc):
        headers = getattr(exc, "headers", None)
        status_code, body = convert_exception_to_status_body(exc)
        return self.request.make_json_response(
            body, status=status_code, headers=headers
        )

    def _make_response(self, status_mapping, headers_tuple, content):
        self.status = status_mapping[:3]
        self.headers = dict(headers_tuple)
        self.inner_exception = None
        # in case of exception, the method asgi_done_callback of the
        # ASGIResponder will trigger an "a2wsgi.error" event with the exception
        # instance stored in a tuple with the type of the exception and the traceback.
        # The event loop will then be notified and then call the `error_response`
        # method of the ASGIResponder. This method will then call the
        # `_make_response` method provided as callback to the app with the tuple
        # of the exception as content. In this case, we store the exception
        # instance in the `inner_exception` attribute to be able to raise it
        # in the `dispatch` method.
        if (
            isinstance(content, tuple)
            and len(content) == 3
            and isinstance(content[1], Exception)
        ):
            self.inner_exception = content[1]

    def _get_environ(self):
        try:
            # normal case after
            # https://github.com/odoo/odoo/commit/cb1d057dcab28cb0b0487244ba99231ee292502e
            httprequest = self.request.httprequest._HTTPRequest__wrapped
        except AttributeError:
            # fallback for older odoo versions
            # The try except is the most efficient way to handle this
            # as we expect that most of the time the attribute will be there
            # and this code will no more be executed if it runs on an up to
            # date odoo version. (EAFP: Easier to Ask for Forgiveness than Permission)
            httprequest = self.request.httprequest
        environ = httprequest.environ
        stream = httprequest._get_stream_for_parsing()
        # Check if the stream supports seeking
        if hasattr(stream, "seekable") and stream.seekable():
            # Reset the stream to the beginning to ensure it can be consumed
            # again by the application in case of a retry mechanism
            stream.seek(0)
        else:
            # If the stream does not support seeking, we need wrap it
            # in a BytesIO object. This way we can seek back to the beginning
            # of the stream to read the data again if needed.
            if not hasattr(httprequest, "_cached_stream"):
                httprequest._cached_stream = BytesIO(stream.read())
            stream = httprequest._cached_stream
            stream.seek(0)
        environ["wsgi.input"] = stream
        return environ

    @contextmanager
    def _manage_odoo_env(self, uid=None):
        env = request.env
        accept_language = request.httprequest.headers.get("Accept-language")
        context = env.context
        if accept_language:
            lang = (
                env["res.lang"].sudo()._get_lang_from_accept_language(accept_language)
            )
            if lang:
                env = env(context=dict(context, lang=lang))

        # add tz in the context
        tz = request.httprequest.headers.get("X-Timezone")
        if tz:
            env = env(context=dict(context, tz=tz))
        if uid:
            env = env(user=uid)
        token = odoo_env_ctx.set(env)

        try:
            yield
        finally:
            odoo_env_ctx.reset(token)

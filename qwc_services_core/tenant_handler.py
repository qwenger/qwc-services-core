from datetime import datetime
import os
import re
from flask import request
from flask.sessions import SecureCookieSessionInterface

from .permissions_reader import PermissionsReader
from .runtime_config import RuntimeConfig


DEFAULT_TENANT = 'default'


class TenantHandlerBase:
    """Tenant handler base class
    """

    def __init__(self):
        self.tenant_name = os.environ.get('QWC_TENANT')
        self.tenant_header = os.environ.get('TENANT_HEADER')
        self.tenant_url_re = os.environ.get('TENANT_URL_RE')
        if self.tenant_url_re:
            self.tenant_url_re = re.compile(self.tenant_url_re)

    def tenant(self):
        if self.tenant_name:
            return self.tenant_name
        if self.tenant_header:
            return request.headers.get(self.tenant_header, DEFAULT_TENANT)
        if self.tenant_url_re:
            match = self.tenant_url_re.match(request.base_url)
            if match:
                return match.group(1)
            else:
                return DEFAULT_TENANT
        return ''


class TenantHandler(TenantHandlerBase):
    """Tenant handler with configuraton cache
    """

    def __init__(self, logger):
        """Constructor

        :param Logger logger: Application logger
        """
        TenantHandlerBase.__init__(self)
        self.logger = logger
        self.handler_cache = {}  # handler_cache[handler_name][tenant]

    def handler(self, service_name, handler_name, tenant):
        """Get service handler for tenant.

        Return None if not yet registered or if config files have changed.

        :param str service_name: Service name
                                 (used for detecting config changes)
        :param str handler_name: Handler name
        :param str tenant: Tenant ID
        """
        handlers = self.handler_cache.get(handler_name)
        if handlers:
            handler = handlers.get(tenant)
            if handler:
                # check for config updates
                last_update = self.last_config_update(service_name, tenant)
                if last_update and last_update < handler.get('last_update'):
                    # cache is up-to-date
                    return handler.get('handler')
                else:
                    # config has changed, remove handler from cache
                    del handlers[tenant]

        return None

    def register_handler(self, handler_name, tenant, handler):
        """Register service handler for tenant"""
        handlers = self.handler_cache.get(handler_name)
        if handlers is None:
            handlers = {}
            self.handler_cache[handler_name] = handlers
        handlers[tenant] = {
            'handler': handler,
            'last_update': datetime.utcnow()
        }
        return handler

    def last_config_update(self, service_name, tenant):
        """Return latest timestamp of config and permission files for a tenant.

        :param str service_name: Service name
        :param str tenant: Tenant ID
        """
        # get latest timestamp of config and permission files
        last_config_update = None
        paths = [
            RuntimeConfig.config_file_path(service_name, tenant),
            PermissionsReader.permissions_file_path(tenant)
        ]
        for path in paths:
            if os.path.isfile(path):
                timestamp = datetime.utcfromtimestamp(
                    os.path.getmtime(path)
                )
                if (
                    last_config_update is None
                    or timestamp > last_config_update
                ):
                    last_config_update = timestamp

        return last_config_update


class TenantPrefixMiddleware(TenantHandlerBase):
    """WSGI middleware injecting tenant header in path"""
    def __init__(self, app, _header=None, _ignore_default=None):
        TenantHandlerBase.__init__(self)
        if self.tenant_header:
            self.header = 'HTTP_' + self.tenant_header.upper()
        else:
            self.header = None
        self.app = app

    def middleware_tenant(self, environ):
        if self.header:
            return environ.get(self.header, DEFAULT_TENANT)
        elif self.tenant_url_re:
            # tenant already in path
            return None
        else:
            return self.tenant()

    def __call__(self, environ, start_response):
        # environ in request http://localhost:9090/base/pages/test.html?arg=1
        # /base is mountpoint (e.g. via WSGIScriptAlias)
        # 'REQUEST_URI': '/base/pages/test.html?arg=1'
        # 'SCRIPT_NAME': '/base'
        # 'PATH_INFO': '/pages/test.html'
        # 'QUERY_STRING': 'arg=1'
        # see also https://www.python.org/dev/peps/pep-3333/#environ-variables
        tenant = self.middleware_tenant(environ)
        if tenant:
            prefix = environ.get('SCRIPT_NAME', '')
            environ['SCRIPT_NAME'] = prefix + '/' + tenant
        return self.app(environ, start_response)


class TenantSessionInterface(SecureCookieSessionInterface):
    """Flask session handler injecting tenant in JWT cookie path"""
    def __init__(self, environ):
        SecureCookieSessionInterface.__init__(self)
        self.prefix = '/'

    def open_session(self, app, request):
        # store service path prefix for cookie path
        self.prefix = request.script_root + '/'
        return SecureCookieSessionInterface.open_session(self, app, request)

    def get_cookie_path(self, app):
        # https://flask.palletsprojects.com/en/1.1.x/api/#flask.sessions.SessionInterface.get_cookie_path
        # Set config as a side effect
        app.config['JWT_ACCESS_COOKIE_PATH'] = self.prefix
        return self.prefix

# Copyright 2012 OpenStack LLC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import copy
import functools
import hashlib
import json
import logging
import time

import requests
import six
import six.moves.urllib.parse as urlparse
from arsenalclient import exc
from arsenalclient.common.i18n import _
from arsenalclient.common.i18n import _LE
from oslo_utils import strutils
from six.moves import http_client

LOG = logging.getLogger(__name__)
USER_AGENT = 'python-arsenalclient'
CHUNKSIZE = 1024 * 64  # 64kB
DEFAULT_VER = '1'

DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_INTERVAL = 2
SENSITIVE_HEADERS = ('X-Auth-Token',)


SUPPORTED_ENDPOINT_SCHEME = ('http', 'https')


def _trim_endpoint_api_version(url):
    return url

def _extract_error_json(body):
    """Return  error_message from the HTTP response body."""
    error_json = {}
    try:
        body_json = json.loads(body)
        if 'error_message' in body_json:
            raw_msg = body_json['error_message']
            error_json = json.loads(raw_msg)
    except ValueError:
        pass

    return error_json


def get_server(endpoint):
    """Extract and return the server & port that we're connecting to."""
    if endpoint is None:
        return None, None
    parts = urlparse.urlparse(endpoint)
    return parts.hostname, str(parts.port)


_RETRY_EXCEPTIONS = (exc.Conflict, exc.ServiceUnavailable,
                     exc.ConnectionRefused)

def with_retries(func):
    """Wrapper for _http_request adding support for retries."""
    @functools.wraps(func)
    def wrapper(self, url, method, **kwargs):
        if self.conflict_max_retries is None:
            self.conflict_max_retries = DEFAULT_MAX_RETRIES
        if self.conflict_retry_interval is None:
            self.conflict_retry_interval = DEFAULT_RETRY_INTERVAL

        num_attempts = self.conflict_max_retries + 1
        for attempt in range(1, num_attempts + 1):
            try:
                return func(self, url, method, **kwargs)
            except _RETRY_EXCEPTIONS as error:
                msg = (_LE("Error contacting Arsenal server: %(error)s. "
                           "Attempt %(attempt)d of %(total)d") %
                       {'attempt': attempt,
                        'total': num_attempts,
                        'error': error})
                if attempt == num_attempts:
                    LOG.error(msg)
                    raise
                else:
                    LOG.debug(msg)
                    time.sleep(self.conflict_retry_interval)

    return wrapper


class HTTPClient():

    def __init__(self, endpoint, **kwargs):
        self.endpoint = endpoint
        self.endpoint_trimmed = _trim_endpoint_api_version(endpoint)
        self.auth_token = kwargs.get('token')
        self.auth_ref = kwargs.get('auth_ref')
        self.api_version_select_state = kwargs.get(
            'api_version_select_state', 'default')
        self.conflict_max_retries = kwargs.pop('max_retries',
                                               DEFAULT_MAX_RETRIES)
        self.conflict_retry_interval = kwargs.pop('retry_interval',
                                                  DEFAULT_RETRY_INTERVAL)
        self.session = requests.Session()

        parts = urlparse.urlparse(endpoint)
        if parts.scheme not in SUPPORTED_ENDPOINT_SCHEME:
            msg = _('Unsupported scheme: %s') % parts.scheme
            raise exc.EndpointException(msg)

        if parts.scheme == 'https':
            if kwargs.get('insecure') is True:
                self.session.verify = False
            elif kwargs.get('ca_file'):
                self.session.verify = kwargs['ca_file']
            self.session.cert = (kwargs.get('cert_file'),
                                 kwargs.get('key_file'))

    def _process_header(self, name, value):
        """Redacts any sensitive header

        Redact a header that contains sensitive information, by returning an
        updated header with the sha1 hash of that value. The redacted value is
        prefixed by '{SHA1}' because that's the convention used within
        OpenStack.

        :returns: A tuple of (name, value)
                  name: the safe encoding format of name
                  value: the redacted value if name is x-auth-token,
                         or the safe encoding format of name

        """
        if name in SENSITIVE_HEADERS:
            v = value.encode('utf-8')
            h = hashlib.sha1(v)
            d = h.hexdigest()
            return (name, "{SHA1}%s" % d)
        else:
            return (name, value)

    def log_curl_request(self, method, url, kwargs):
        curl = ['curl -i -X %s' % method]

        for (key, value) in kwargs['headers'].items():
            header = '-H \'%s: %s\'' % self._process_header(key, value)
            curl.append(header)

        if not self.session.verify:
            curl.append('-k')
        elif isinstance(self.session.verify, six.string_types):
            curl.append('--cacert %s' % self.session.verify)

        if self.session.cert:
            curl.append('--cert %s' % self.session.cert[0])
            curl.append('--key %s' % self.session.cert[1])

        if 'body' in kwargs:
            body = strutils.mask_password(kwargs['body'])
            curl.append('-d \'%s\'' % body)

        curl.append(urlparse.urljoin(self.endpoint_trimmed, url))
        LOG.debug(' '.join(curl))

    @staticmethod
    def log_http_response(resp, body=None):
        # NOTE(aarefiev): resp.raw is urllib3 response object, it's used
        # only to get 'version', response from request with 'stream = True'
        # should be used for raw reading.
        status = (resp.raw.version / 10.0, resp.status_code, resp.reason)
        dump = ['\nHTTP/%.1f %s %s' % status]
        dump.extend(['%s: %s' % (k, v) for k, v in resp.headers.items()])
        dump.append('')
        if body:
            body = strutils.mask_password(body)
            dump.extend([body, ''])
        LOG.debug('\n'.join(dump))

    def _make_connection_url(self, url):
        return urlparse.urljoin(self.endpoint_trimmed, url)

    def _parse_version_headers(self, resp):
        return self._generic_parse_version_headers(resp.headers.get)

    def _make_simple_request(self, conn, method, url):
        return conn.request(method, self._make_connection_url(url))

    @with_retries
    def _http_request(self, url, method, **kwargs):
        """Send an http request with the specified characteristics.

        Wrapper around request.Session.request to handle tasks such
        as setting headers and error handling.
        """
        # Copy the kwargs so we can reuse the original in case of redirects
        kwargs['headers'] = copy.deepcopy(kwargs.get('headers', {}))
        kwargs['headers'].setdefault('User-Agent', USER_AGENT)
        if self.auth_token:
            kwargs['headers'].setdefault('X-Auth-Token', self.auth_token)

        self.log_curl_request(method, url, kwargs)

        # NOTE(aarefiev): This is for backwards compatibility, request
        # expected body in 'data' field, previously we used httplib,
        # which expected 'body' field.
        body = kwargs.pop('body', None)
        if body:
            kwargs['data'] = body

        conn_url = self._make_connection_url(url)
        try:
            resp = self.session.request(method,
                                        conn_url,
                                        **kwargs)

            # TODO(deva): implement graceful client downgrade when connecting
            # to servers that did not support microversions. Details here:
            # http://specs.openstack.org/openstack/ironic-specs/specs/kilo/api-microversions.html#use-case-3b-new-client-communicating-with-a-old-ironic-user-specified  # noqa

            if resp.status_code == http_client.NOT_ACCEPTABLE:
                negotiated_ver = self.negotiate_version(self.session, resp)
                kwargs['headers']['X-OpenStack-Arsenal-API-Version'] = (
                    negotiated_ver)
                return self._http_request(url, method, **kwargs)

        except requests.exceptions.RequestException as e:
            message = (_("Error has occurred while handling "
                       "request for %(url)s: %(e)s") %
                       dict(url=conn_url, e=e))
            # NOTE(aarefiev): not valid request(invalid url, missing schema,
            # and so on), retrying is not needed.
            if isinstance(e, ValueError):
                raise exc.ValidationError(message)

            raise exc.ConnectionRefused(message)

        body_iter = resp.iter_content(chunk_size=CHUNKSIZE)

        # Read body into string if it isn't obviously image data
        body_str = None
        if resp.headers.get('Content-Type') != 'application/octet-stream':
            body_str = ''.join([chunk.decode() for chunk in body_iter])
            self.log_http_response(resp, body_str)
            body_iter = six.StringIO(body_str)
        else:
            self.log_http_response(resp)

        if resp.status_code >= http_client.BAD_REQUEST:
            error_json = _extract_error_json(body_str)
            raise exc.from_response(
                resp, error_json.get('faultstring'),
                error_json.get('debuginfo'), method, url)
        elif resp.status_code in (http_client.MOVED_PERMANENTLY,
                                  http_client.FOUND,
                                  http_client.USE_PROXY):
            # Redirected. Reissue the request to the new location.
            return self._http_request(resp['location'], method, **kwargs)
        elif resp.status_code == http_client.MULTIPLE_CHOICES:
            raise exc.from_response(resp, method=method, url=url)

        return resp, body_iter

    def json_request(self, method, url, **kwargs):
        kwargs.setdefault('headers', {})
        kwargs['headers'].setdefault('Content-Type', 'application/json')
        kwargs['headers'].setdefault('Accept', 'application/json')

        if 'body' in kwargs:
            kwargs['body'] = json.dumps(kwargs['body'])

        resp, body_iter = self._http_request(url, method, **kwargs)
        content_type = resp.headers.get('Content-Type')

        if (resp.status_code in (http_client.NO_CONTENT,
                                 http_client.RESET_CONTENT)
                or content_type is None):
            return resp, list()

        if 'application/json' in content_type:
            body = ''.join([chunk for chunk in body_iter])
            try:
                body = json.loads(body)
            except ValueError:
                LOG.error(_LE('Could not decode response body as JSON'))
        else:
            body = None

        return resp, body

    def raw_request(self, method, url, **kwargs):
        kwargs.setdefault('headers', {})
        kwargs['headers'].setdefault('Content-Type',
                                     'application/octet-stream')
        return self._http_request(url, method, **kwargs)


class SessionClient():
    """HTTP client based on Keystone client session."""

    def __init__(self,
                 max_retries,
                 retry_interval,
                 endpoint,
                 **kwargs):
        self.conflict_max_retries = max_retries
        self.conflict_retry_interval = retry_interval
        self.endpoint = endpoint

        super(SessionClient, self).__init__(**kwargs)

    def _parse_version_headers(self, resp):
        return self._generic_parse_version_headers(resp.headers.get)

    def _make_simple_request(self, conn, method, url):
        # NOTE: conn is self.session for this class
        return conn.request(url, method, raise_exc=False)

    @with_retries
    def _http_request(self, url, method, **kwargs):
        kwargs.setdefault('user_agent', USER_AGENT)
        kwargs.setdefault('auth', self.auth)
        if isinstance(self.endpoint_override, six.string_types):
            kwargs.setdefault(
                'endpoint_override',
                _trim_endpoint_api_version(self.endpoint_override)
            )

        resp = self.session.request(url, method,
                                    raise_exc=False, **kwargs)
        if resp.status_code >= http_client.BAD_REQUEST:
            error_json = _extract_error_json(resp.content)
            raise exc.from_response(resp, error_json.get('faultstring'),
                                    error_json.get('debuginfo'), method, url)
        elif resp.status_code in (http_client.MOVED_PERMANENTLY,
                                  http_client.FOUND, http_client.USE_PROXY):
            # Redirected. Reissue the request to the new location.
            location = resp.headers.get('location')
            resp = self._http_request(location, method, **kwargs)
        elif resp.status_code == http_client.MULTIPLE_CHOICES:
            raise exc.from_response(resp, method=method, url=url)
        return resp

    def json_request(self, method, url, **kwargs):
        kwargs.setdefault('headers', {})
        kwargs['headers'].setdefault('Content-Type', 'application/json')
        kwargs['headers'].setdefault('Accept', 'application/json')

        if 'body' in kwargs:
            kwargs['data'] = json.dumps(kwargs.pop('body'))

        resp = self._http_request(url, method, **kwargs)
        body = resp.content
        content_type = resp.headers.get('content-type', None)
        status = resp.status_code
        if (status in (http_client.NO_CONTENT, http_client.RESET_CONTENT) or
                content_type is None):
            return resp, list()
        if 'application/json' in content_type:
            try:
                body = resp.json()
            except ValueError:
                LOG.error(_LE('Could not decode response body as JSON'))
        else:
            body = None

        return resp, body

    def raw_request(self, method, url, **kwargs):
        kwargs.setdefault('headers', {})
        kwargs['headers'].setdefault('Content-Type',
                                     'application/octet-stream')
        return self._http_request(url, method, **kwargs)


def _construct_http_client(endpoint=None,
                           max_retries=DEFAULT_MAX_RETRIES,
                           retry_interval=DEFAULT_RETRY_INTERVAL,
                           timeout=600,
                           **kwargs):
    return HTTPClient(endpoint=endpoint,
                      max_retries=max_retries,
                      retry_interval=retry_interval,
                      timeout=timeout)

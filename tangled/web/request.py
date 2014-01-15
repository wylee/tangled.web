import logging
import posixpath
import traceback
from urllib.parse import quote, quote_plus, urlencode, urlparse

from webob import BaseRequest
from webob.exc import status_map, WSGIHTTPException

from tangled.decorators import reify, cached_property

from .abcs import AHelpers, AMountedResource, ARequest, AResponse


log = logging.getLogger(__name__)


STATUS_MAP = {
    'DELETE': 204,
    'GET': 200,
    'HEAD': 204,
    'OPTIONS': 200,
    'POST': 303,
    'PUT': 204,
}



class Request(ARequest, BaseRequest):

    """Default request factory.

    Every request has a reference to its application context.

    """

    def __init__(self, environ, app, *args, **kwargs):
        super().__init__(environ, *args, **kwargs)
        self.app = app

    def get_setting(self, *args, **kwargs):
        return self.app.get_setting(*args, **kwargs)

    @reify
    def helpers(self):
        helpers_factory = self.app.get_required(AHelpers)
        helpers = self.app.get_all('helper', default={}, as_dict=True)
        return type('Helpers', (helpers_factory,), helpers)(self.app, self)

    # Response related

    @reify
    def response(self):
        """Create the default response object for this request.

        The response is initialized with attributes set via
        ``@represent``: ``status``, ``location``, and
        ``response_attrs``.

        .. note:: See note in :meth:`representation_info`.

        """
        info = self.representation_info
        args = {}
        args.update(info.response_attrs)
        if info.status:
            args['status'] = info.status
        else:
            args['status'] = STATUS_MAP[self.method]
        if info.location:
            location = info.location
            if location == 'REFERER':
                location = self.referer
            elif location == 'CAME_FROM':
                location = self.params.get('came_from')
                location = location or self.make_url('/')
                log.debug('Came from: {}'.format(location))
            else:
                url_info = urlparse(location)
                if url_info.netloc:
                    if not url_info.scheme:
                        location = '{0.scheme}{1}'.format(self, location)
                else:
                    location = self.make_url(location)
            args['location'] = location
        return self.app.get(AResponse)(**args)

    def update_response(self, **kwargs):
        """Set multiple attributes on `request.response`."""
        response = self.response
        for name, value in kwargs.items():
            setattr(response, name, value)

    @reify
    def response_content_type(self):
        """Get the content type to use for the response.

        The best matching type from the set of registered content types
        will be selected. If there's no match, the default type will
        be used instead.

        """
        content_types = self.app.get_all('content_type')
        content_type = self.accept.best_match(content_types)
        if not content_type:
            content_type = self.get_setting('default_content_type')
        return content_type

    @cached_property
    def representation_info(self):
        """Get info for the resource associated with this request.

        .. note:: This can't be safely accessed until after the resource
                  has been found and set for this request. In particular,
                  ``resource`` needs to be set.

        """
        return self.app.get_representation_info(
            self.resource, self.method, self.response_content_type)

    # URL generators

    def make_url(self, path, query=None, fragment=None, *,
                 _fully_qualified=True):
        """Generate a URL.

        ``path`` should be application-relative (that is, it should
        *not* include SCRIPT_NAME).

        ``query`` can be a string, a dict, or a sequence. See
        :meth:`make_query_string` for details.

        If ``fragment`` is passed it will be quoted using
        :func:`urllib.parse.quote` with no "safe" characters (i.e.,
        all special characters will be quoted).

        """
        path = path.lstrip('/')
        if _fully_qualified:
            base = self.application_url
        else:
            base = self.script_name
            if not base.startswith('/'):
                base = '/' + base
        url = posixpath.join(base, path)
        if query is not None:
            url += self.make_query_string(query)
        if fragment is not None:
            url += '#' + quote(fragment.lstrip('#'), safe='')
        return url

    def make_path(self, *args, **kwargs):
        return self.make_url(*args, _fully_qualified=False, **kwargs)

    def make_query_string(self, query, doseq=True, safe='&+=',
                          encoding='utf-8', errors=None):
        """Convert ``query`` to a quoted query string.

        If ``query`` is not a string, :func:`urllib.parse.urlencode`
        will be called to convert it to a string (it can be a dict or
        sequence of two-element tuples).

        If ``query`` is a string, it should *not* already be quoted,
        with the exception of "&", "+", and "=" (i.e., the ``safe``
        characters. Special characters in keys and values will be quoted
        via :func:`urllib.parse.quote_plus`.

        ``doseq`` is passed to ``urlencode``.

        ``safe``, ``encoding``, and ``errors`` are passed to both
        ``quote_plus`` and ``urlencode`` (the latter passes them on to
        the former).

        A query string with a "?" prepended will be returned.

        """
        if query is None:
            return '?'
        elif isinstance(query, str):
            query = quote_plus(
                query.lstrip('?'), safe=safe, encoding=encoding, errors=errors)
        else:
            query = urlencode(
                query, doseq=doseq, safe=safe, encoding=encoding,
                errors=errors)
        return '?' + query

    def resource_url(self, resource, urlvars=None, **kwargs):
        """Generate a URL for a resource."""
        if isinstance(resource, str):
            name = resource
        else:
            name = resource.name
        mounted_resource = self.app.get(AMountedResource, name)
        path = mounted_resource.format_path(**(urlvars or {}))
        return self.make_url(path, **kwargs)

    def resource_path(self, resource, urlvars=None, **kwargs):
        """Generate a URL path (with SCRIPT_NAME) for a resource."""
        return self.resource_url(
            resource, urlvars, _fully_qualified=False, **kwargs)

    def static_url(self, path, query=None, **kwargs):
        """Generate a static URL from ``path``.

        ``path`` should always be an application-relative path like
        '/static/images/logo.png'. SCRIPT_NAME will be prepended by
        :meth:`make_url`.

        The first segment in the path ('static' in the example above)
        will be matched against static directories registered via
        :meth:`.app.Application:mount_static_directory`.

        """
        prefix, *rel_path = path.lstrip('/').split('/')
        try:
            directory = self.app.get('static_directory', prefix)
        except KeyError:
            raise ValueError(
                'Unregistered static directory: {}'.format(prefix))
        if isinstance(directory, str):
            # E.g., http://assets.example.com/static/images/logo.png
            url = posixpath.join(directory, *rel_path)
            if query:
                url += self.make_query_string(query)
        else:
            # E.g., /static/images/logo.png
            path = posixpath.join(prefix, *rel_path)
            url = self.make_url(path, query, **kwargs)
        return url

    def static_path(self, *args, **kwargs):
        return self.static_url(*args, _fully_qualified=False, **kwargs)

    # Finished callbacks

    def on_finished(self, callback, *args, **kwargs):
        """Add a finished callback.

        Callbacks must have the signature ``(app, response)``. They
        can also take additional positional and keyword args--``*args``
        and ``**kwargs`` will be passed along to the ``callback``.

        Finished callbacks are always called regardless of whether an
        error occurred while processing the request. They are called
        just before the Tangled application returns to its caller.

        *All* finished callbacks will be called. If any of them raises
        an exception, a :class:`RequestFinishedException` will be raised
        and a "500 Internal Server Error" response will be returned in
        place of the original response.

        Raising instances of :class:`webob.exc.WSGIHTTPException` in
        finished callbacks is an error.

        The ``response`` object can be inspected to see if an error
        occurred while processing the request. If the ``response`` is
        ``None``, the request failed hard (i.e., there was an uncaught
        exception before the response could be created).

        This can be used as a decorator in the simple case where the
        ``callback`` doesn't take any additional args.

        """
        self._finished_callbacks.append((callback, args, kwargs))

    @reify
    def _finished_callbacks(self):
        return []

    def _call_finished_callbacks(self, response):
        """Call finished callbacks in the order they were added.

        See :meth:`on_finished`.

        """
        exceptions = []
        for (callback, args, kwargs) in self._finished_callbacks:
            try:
                try:
                    callback(self.app, self, response, *args, **kwargs)
                except WSGIHTTPException as http_exc:
                    raise ValueError(
                        'WSGIHTTPExceptions cannot be raised in finished '
                        'callbacks ({})'.format(http_exc))
            except Exception as exc:
                exceptions.append(exc)
        if exceptions:
            raise RequestFinishedException(*exceptions)

    # Miscellaneous

    def abort(self, status_code, *args, **kwargs):
        """Abort the request by raising a WSGIHTTPException.

        This is a convenience so resource modules don't need to import
        exceptions from webob.exc.

        """
        response_type = status_map[status_code]
        raise response_type(*args, **kwargs)


class RequestFinishedException(Exception):

    """Wrapper around exceptions raised in finished callbacks.

    See :meth:`Request._call_finished_callbacks`.

    """

    def __init__(self, *exceptions):
        self.exceptions = exceptions
        super().__init__(*exceptions)

    def __str__(self):
        num_exceptions = len(self.exceptions)
        s = '' if num_exceptions == 1 else 's'
        message = [
            '{} exception{} occurred in finished callbacks\n'
            .format(num_exceptions, s)]
        for i, exc in enumerate(self.exceptions, 1):
            tb = traceback.format_exception(
                exc.__class__, exc, exc.__traceback__)
            tb = '\n'.join(tb)
            message.append('{}. {}'.format(i, tb))
        return '\n'.join(message)

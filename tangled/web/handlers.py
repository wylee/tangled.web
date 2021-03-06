"""System handlers.

Requests are processed through a chain of handlers. This module contains
the "system" handlers. These are handlers that always run in a specific
order.

Most of the system handlers *always* run. They can't be turned off, but
you can swap in different implementations via settings. Take a look at
:file:`tangled/web/defaults.ini` to see how you would do this.

Some handlers are only enabled when certain settings are enabled or when
certain configuration takes place. For example, to enable CSRF
protection, the ``tangled.app.csrf.enabled`` setting needs to be set to
``True``. Another example: the static files handlers is only enabled
when at least one static directory has been mounted.

If an auth handler is enabled, it will run directly before any (other)
handlers added by the application developer.

All added handlers are called in the order they were added. The last
handler to run is always the :func:`main` handler; it calls into
application code (i.e., it calls a resource method to get data or
a response).

"""
import logging
import os
import pdb
import sys
import time
import traceback

from webob.exc import WSGIHTTPException, HTTPInternalServerError

from tangled.util import load_object

from . import abcs, csrf
from .events import NewRequest, ResourceFound, NewResponse
from .exc import DebugHTTPInternalServerError
from .representations import Representation
from .response import Response
from .resource.exc import BindError


log = logging.getLogger(__name__)


def exc_handler(app, request, next_handler):
    try:
        return next_handler(app, request)
    except WSGIHTTPException as exc:
        response = exc
    except Exception as exc:
        app.log_exc(request, exc)
        if app.debug:
            if app.settings.get('debug.pdb', False):
                pdb.post_mortem(exc.__traceback__)
            response = DebugHTTPInternalServerError(traceback.format_exc())
        else:
            response = HTTPInternalServerError()
    return get_exc_response(app, request, response)


def get_exc_response(app, request, original_response):
    """Get response for exception.

    If the response's status code is 400 or greater *and* an error
    resource is configured, the error resource's ``GET`` method will be
    called to get the final response. This is accomplished by setting
    the error resource as the resource for the request and then passing
    the request back into the main handler.

    When the response's status code is less than 400 *or* no error
    resource is configured, the response will be returned as is.

    If CORS is enabled, the appropriate CORS headers will be added by
    calling the configured CORS handler.

    """
    try:
        # TODO: Move this?
        if original_response.status_code == 404 and app.debug and not app.testing:
            out = ['"{request.path}" not found; searched:\n\n'.format_map(locals())]
            mounted_resources = app.get_all(abcs.AMountedResource, as_dict=True)

            if mounted_resources:
                def append_entry(name, methods, method, path, *, placeholder='',
                                 longest=len(max('NAME', *mounted_resources, key=len))):
                    methods = ', '.join(sorted(methods))
                    method = ' ({method})'.format_map(locals()) if method else ''
                    out.append('    {name:<{longest}} => {methods}{method}\n'.format_map(locals()))
                    out.append('    {placeholder:<{longest}}    {path}\n'.format_map(locals()))

                append_entry('NAME', ['METHODS'], None, '/PATH')

                for mounted in mounted_resources.values():
                    append_entry(mounted.name, mounted.methods, mounted.method, mounted.path)
            else:
                out.append('    No resources mounted\n')

            print(''.join(out), file=sys.stderr)

        cors_enabled = app.get_setting('cors.enabled')

        if cors_enabled:
            cors_handler = app.get_setting('handler.cors')

        error_resource = app.get_setting('error_resource')

        if (original_response.status_code > 400 and
                error_resource and
                not isinstance(original_response, DebugHTTPInternalServerError)):
            resource = error_resource(app, request)
            request.method = 'GET'
            request.resource = resource
            request.resource_method = 'GET'
            request.response = original_response
            request.resource_args = None

            del request.response_content_type
            del request.resource_config

            main_handler = app.get_setting('handler.main')
            main_handler = HandlerWrapper(main_handler, None)

            if cors_enabled:
                handler = HandlerWrapper(cors_handler, main_handler)
            else:
                handler = main_handler

            try:
                return handler(app, request)
            except WSGIHTTPException as exc:
                app.log_exc(request, exc)
                return exc
        elif cors_enabled:
            next_handler = lambda app, request: original_response
            handler = HandlerWrapper(cors_handler, next_handler)
            return handler(app, request)

    except Exception as exc:
        app.log_exc(request, exc)

    # If there's an exception in the error resource (or there's no error
    # resource configured), the original exception response will be
    # returned, which is better than nothing.
    return original_response


def request_finished_handler(app, request, _):
    """Call request finished callbacks in exc handling context.

    This calls the request finished callbacks in the same exception
    handling context as the request. This way, if exceptions occur in
    finished callbacks, they can be logged and displayed as usual.

    .. note:: Finished callbacks are not called for static requests.

    """
    if not getattr(request, 'is_static', False):
        request._call_finished_callbacks(request.response)
    return request.response


def static_files(app, request, next_handler):
    prefix, rel_path = app._find_static_directory(request.path_info)
    if prefix is not None:
        request.is_static = True
        environ = request.environ.copy()
        environ['PATH_INFO'] = '/' + '/'.join(rel_path)
        static_request = app.make_request(environ)
        directory_app = app.get('static_directory', prefix)
        return directory_app(static_request)
    return next_handler(app, request)


def tweaker(app, request, next_handler):
    """Tweak the request based on special request parameters."""
    specials = {
        '$method': None,
        '$accept': None,
    }
    for k in request.params:
        if k in specials:
            specials[k] = request.params[k]
    for k in specials:
        if k in request.GET:
            del request.GET[k]
        if k in request.POST:
            del request.POST[k]

    if specials['$method']:
        method = specials['$method']
        tunneled_methods = app.get_setting('tunnel_over_post')

        if method == 'DELETE':
            # Changing request.method to DELETE makes request.POST
            # inaccessible.
            token = csrf.get_token(request)
            header = csrf.get_header(request)
            if token in request.POST and header not in request.headers:
                request.headers[header] = request.POST[token]

        if request.method == 'POST' and method in tunneled_methods:
            request.method = method
        elif app.debug:
            request.method = method
        else:
            request.abort(
                400, detail="Can't tunnel {} over POST".format(method))

    if specials['$accept']:
        request.accept = specials['$accept']
    elif app.settings['tangled.app.set_accept_from_ext']:
        root, ext = os.path.splitext(request.path_info)
        if ext:
            repr_type = app.get(Representation, ext.lstrip('.'))
            if repr_type is not None:
                request.accept = repr_type.content_type
                request.path_info = root

    return next_handler(app, request)


def notifier(app, request, next_handler):
    app.notify_subscribers(NewRequest, app, request)
    response = next_handler(app, request)
    app.notify_subscribers(NewResponse, app, request, response)
    return response


def resource_finder(app, request, next_handler):
    """Find resource for request.

    If a resource isn't found, a 404 response is immediately returned.
    If a resource is found but doesn't respond to the request's method,
    a ``405 Method Not Allowed`` response is returned.

    Sets ``request.resource`` and ``request.resource_method``. Notifies
    :class:`ResourceFound` subscribers.

    """
    match = app.find_mounted_resource(request.method, request.path)

    if match is None:
        match = app.find_mounted_resource(request.method, request.path, ignore_method=True)
        if match is None:
            # No resources mounted at path
            request.abort(404)
        # One or more resources mounted at path but none respond to
        # request method
        request.abort(405)

    mounted_resource, request.urlvars = match

    if mounted_resource.add_slash and not request.path_info.endswith('/'):
        request.path_info = '{request.path_info}/'.format_map(locals())
        request.abort(303, location=request.url)

    resource = mounted_resource.factory(app, request, mounted_resource.name)
    method = mounted_resource.method or request.method

    try:
        resource_args = resource.bind(request, method)
    except BindError as exc:
        request.abort(400, str(exc))

    request.resource = resource
    request.resource_method = method
    request.resource_args = resource_args

    app.notify_subscribers(ResourceFound, app, request, resource, method, resource_args)
    return next_handler(app, request)


# csrf handler will be inserted here if enabled
# auth handler will be inserted here if enabled
# non-system handlers will be inserted here
# cors handler will be inserted here if enabled


def timer(app, request, next_handler):
    """Log time taken to handle a request."""
    start_time = time.time()
    response = next_handler(app, request)
    elapsed_time = (time.time() - start_time) * 1000
    log.debug('Request to {} took {:.2f}ms'.format(request.url, elapsed_time))
    return response


def main(app, request, _):
    """Get data from resource method and return response.

    If the resource method returns a response object (an instance of
    :class:`Response`), that response will be returned without further
    processing.

    If the status of ``request.response`` has been set to 3xx (either
    via @config or in the body of the resource method) AND the resource
    method returns no data, the response will will be returned as is
    without further processing.

    Otherwise, a representation will be generated based on the request's
    Accept header (unless a representation type has been set via
    @config, in which case that type will be used instead of doing
    a best match guess).

    If the representation returns a response object as its content, that
    response will be returned without further processing.

    Otherwise, `request.response` will be updated according to the
    representation type (the response's content_type, charset, and body
    are set from the representation).

    """
    method = getattr(request.resource, request.resource_method)
    resource_args = getattr(request, 'resource_args', None)

    if resource_args is None:
        data = method()
    else:
        args = request.resource_args.args
        kwargs = request.resource_args.kwargs
        data = method(*args, **kwargs)

    if isinstance(data, Response):
        return data

    response = request.response

    if 300 <= response.status_code < 400 and data is None:
        return response

    info = request.resource_config
    log.debug(info)

    if info.type:
        differentiator = info.type
    else:
        differentiator = request.response_content_type
    repr_type = app.get_required(Representation, differentiator)

    kwargs = info.representation_args
    representation = repr_type(app, request, data, **kwargs)

    if isinstance(representation.content, Response):
        return representation.content

    response.content_type = representation.content_type
    response.charset = representation.encoding
    response.text = representation.content
    return response


class HandlerWrapper:

    # An internal class used for wrapping handler callables.

    def __init__(self, callable_, next_handler):
        if isinstance(callable_, str):
            callable_ = load_object(callable_)
        self.callable_ = callable_
        self.next = next_handler

    def __call__(self, app, request):
        response = self.callable_(app, request, self.next)
        if response is None:
            raise ValueError('Handler returned None')
        return response

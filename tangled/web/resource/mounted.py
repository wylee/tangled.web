import posixpath
import re
from collections import namedtuple

from tangled.converters import as_tuple
from tangled.decorators import reify
from tangled.util import load_object


Match = namedtuple('Match', ('name', 'factory', 'path', 'urlvars'))


class MountedResource:

    identifier = r'{(?!.*\d+.*)(\w+)}'
    identifier_with_re = r'{(?!.*\d+.*)(\w+):(.*)}'

    def __init__(self, name, factory, path, methods=()):
        self.name = name
        self.factory = load_object(factory, level=4)
        self.path = path
        self.methods = set(as_tuple(methods, sep=','))
        self.path_regex  # Ensure valid

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_traceback):
        return False

    @reify
    def path_regex(self):
        path = self.path
        if not path.startswith('/'):
            path = '/' + path
        regex = re.sub(self.identifier, r'(?P<\1>[\w-]+)', path)
        regex = re.sub(self.identifier_with_re, r'(?P<\1>\2)', regex)
        regex = r'^{}$'.format(regex)
        regex = re.compile(regex)
        return regex

    @reify
    def format_string(self):
        path = self.path
        if not path.startswith('/'):
            path = '/' + path
        format_string = re.sub(self.identifier, r'{\1}', path)
        format_string = re.sub(self.identifier_with_re, r'{\1}', format_string)
        return format_string

    def match(self, method, path):
        if self.methods and method not in self.methods:
            return None
        match = self.path_regex.search(path)
        if match:
            return Match(self.name, self.factory, self.path, match.groupdict())

    def match_request(self, request):
        return self.match(request.method, request.path_info)

    def format_path(self, **args):
        """Format the resource path with the specified args."""
        path = self.format_string.format(**args)
        if not self.path_regex.search(path):
            raise ValueError(
                'Invalid substitions: {} for {}'.format(args, path))
        return path

    def mount(self, name, factory, path):
        """Mount subresource.

        This can be used like this::

            r = app.mount_resource(...)
            r.mount(...)

        and like this::

            with app.mount_resource(...) as r:
                r.mount(...)

        In either case, the subresource's path will be prepended with
        its parent's path.

        """
        path = posixpath.join(self.path, path.lstrip('/'))
        return self.container.mount(name, factory, path)
"""Jedi mini server for deoplete-jedi

This script allows Jedi to run using the Python interpreter that is found in
the user's environment instead of the one Neovim is using.

Jedi seems to accumulate latency with each completion.  To deal with this, the
server is restarted after 50 completions.  This threshold is relatively high
considering that deoplete-jedi caches completion results.  These combined
should make deoplete-jedi's completions pretty fast and responsive.
"""
from __future__ import unicode_literals

import os
import sys
import struct
import argparse
import subprocess
import logging

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler)

try:
    import cPickle as pickle
except ImportError:
    import pickle

jedi_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'jedi')

# Type mapping.  Empty values will use the key value instead.
# Keep them 5 characters max to minimize required space to display.
_types = {
    'import': 'imprt',
    'class': '',
    'function': 'def',
    'globalstmt': 'var',
    'instance': 'var',
    'statement': 'var',
    'keyword': 'keywd',
    'module': 'mod',
    'param': 'arg',
    'property': 'prop',

    'bool': '',
    'bytes': 'byte',
    'complex': 'cmplx',
    'dict': '',
    'list': '',
    'float': '',
    'int': '',
    'object': 'obj',
    'set': '',
    'slice': '',
    'str': '',
    'tuple': '',
    'mappingproxy': 'dict',  # cls.__dict__
    'member_descriptor': 'cattr',
    'getset_descriptor': 'cprop',
    'method_descriptor': 'cdef',
}


class StreamError(Exception):
    """Error in reading/writing streams."""


class StreamEmpty(StreamError):
    """Empty stream data"""


def stream_read(pipe):
    """Read data from the pipe."""
    buffer = getattr(pipe, 'buffer', pipe)
    header = buffer.read(4)
    if not header:
        raise StreamEmpty

    if len(header) < 4:
        raise StreamError('Incorrect byte length')

    length = struct.unpack('I', header)[0]
    data = buffer.read(length)
    if len(data) < length:
        raise StreamError('Got less data than expected')
    return pickle.loads(data)


def stream_write(pipe, obj):
    """Write data to the pipe."""
    data = pickle.dumps(obj, 2)
    header = struct.pack('I', len(data))
    buffer = getattr(pipe, 'buffer', pipe)
    buffer.write(header + data)
    pipe.flush()


class Server(object):
    """Server class

    This is created when this script is ran directly.
    """
    def __init__(self, desc_len=0, short_types=False, show_docstring=False):
        self.desc_len = desc_len
        self.use_short_types = short_types
        self.show_docstring = show_docstring

    def _loop(self):
        import jedi
        while True:
            data = stream_read(sys.stdin)
            if not isinstance(data, tuple):
                break
            source, line, col, filename = data
            log.debug('Line: %r, Col: %r, Filename: %r', line, col, filename)
            completions = jedi.Script(source, line, col, filename).completions()
            out = []
            tmp_filecache = {}
            for c in completions:
                name, type_, desc, abbr = self.parse_completion(c, tmp_filecache)
                kind = type_ if not self.use_short_types \
                    else _types.get(type_) or type_
                out.append((c.module_path, name, type_, desc, abbr, kind))
            stream_write(sys.stdout, out)

    def run(self):
        log.debug(sys.path)
        try:
            self._loop()
        except StreamEmpty:
            log.debug('Input closed')
        except Exception:
            log.exception('exception')

    def call_signature(self, comp):
        """Construct the function's call signature.

        comp.docstring() is not reliable and we don't need the entire
        docstring.

        Returns a tuple of (full, abbr) call signatures.
        """
        params = []
        params_abbr = []
        try:
            # Total length includes parenthesis
            length = len(comp.name)
            for i, p in enumerate(comp.params):
                desc = p.description.strip()
                if i == 0 and desc == 'self':
                    continue

                if '\\n' in desc:
                    desc = desc.replace('\\n', '\\x0A')

                length += len(desc) + 2
                params.append(desc)

            params_abbr = params[:]
            if self.desc_len > 0:
                if length > self.desc_len:
                    # First remove all keyword params to see if that makes it
                    # short enough.
                    params_abbr = [x.split('=', 1)[0] for x in params_abbr]
                    length = len(comp.name) + sum([len(x) + 2
                                                   for x in params_abbr])

                while length + 3 > self.desc_len \
                        and len(params_abbr):
                    # Keep removing params until short enough.
                    length -= len(params_abbr[-1]) - 3
                    params_abbr = params_abbr[:-1]
                if len(params) > len(params_abbr):
                    params_abbr.append('...')
        except AttributeError:
            pass

        return ('{}({})'.format(comp.name, ', '.join(params)),
                '{}({})'.format(comp.name, ', '.join(params_abbr)))

    def parse_completion(self, comp, cache):
        """Return a tuple describing the completion.

        Returns (name, type, description, abbreviated)
        """
        name = comp.name
        type_, desc = [x.strip() for x in comp.description.split(':', 1)]

        if type_ == 'instance' and desc.startswith(('builtins.', 'posix.')):
            # Simple description
            builtin_type = desc.rsplit('.', 1)[-1]
            if builtin_type in _types:
                return (name, builtin_type, '', '')

        if type_ == 'class' and desc.startswith('builtins.'):
            if self.show_docstring:
                return (name,
                        type_,
                        comp.docstring(),
                        self.call_signature(comp)[1])
            else:
                return (name, type_) + self.call_signature(comp)

        if type_ == 'function':
            if comp.module_path not in cache and comp.line and comp.line > 1 \
                    and os.path.exists(comp.module_path):
                with open(comp.module_path, 'r') as fp:
                    cache[comp.module_path] = fp.readlines()
            lines = cache.get(comp.module_path)
            if isinstance(lines, list) and len(lines) > 1 \
                    and comp.line < len(lines) and comp.line > 1:
                # Check the function's decorators to check if it's decorated
                # with @property
                i = comp.line - 2
                while i >= 0:
                    line = lines[i].lstrip()
                    if not line.startswith('@'):
                        break
                    if line.startswith('@property'):
                        return (name, 'property', desc, '')
                    i -= 1
            if self.show_docstring:
                return (name,
                        type_,
                        comp.docstring(),
                        self.call_signature(comp)[1])
            else:
                return (name, type_) + self.call_signature(comp)

        return (name, type_, '', '')


class Client(object):
    """Client object

    This will be used by deoplete-jedi to interact with the server.
    """
    max_completion_count = 50

    def __init__(self, desc_len=0, short_types=False, show_docstring=False,
                 debug=False):
        self._server = None
        self._count = 0
        self.env = os.environ.copy()
        self.env.update({
            'PYTHONPATH': jedi_path,
        })

        if 'VIRTUAL_ENV' in os.environ:
            self.env['VIRTUAL_ENV'] = os.getenv('VIRTUAL_ENV')

        self.cmd = ['python', '-u', __file__, '--desc-length', str(desc_len)]
        if short_types:
            self.cmd.append('--short-types')
        if show_docstring:
            self.cmd.append('--docstrings')
        if debug:
            self.cmd.append('--debug')

        self.restart()

    def shutdown(self):
        """Shut down the server."""
        if self._server is not None and self._server.returncode is None:
            # Closing the server's stdin will cause it to exit.
            self._server.stdin.close()

    def restart(self):
        """Start or restart the server

        If a server is already running, shut it down.
        """
        self.shutdown()
        self._server = subprocess.Popen(self.cmd, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, env=self.env)

    def completions(self, *args):
        """Get completions from the server.

        If the number of completions already performed reaches a threshold,
        restart the server.
        """
        if self._count > self.max_completion_count:
            self._count = 0
            self.restart()

        self._count += 1
        stream_write(self._server.stdin, args)
        return stream_read(self._server.stdout)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--desc-length', type=int)
    parser.add_argument('--short-types', action='store_true')
    parser.add_argument('--docstrings', action='store_true')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    if args.debug:
        log.removeHandler(logging.NullHandler)
        handler = logging.FileHandler('/tmp/jedi-server.log')
        handler.setLevel(logging.DEBUG)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)

    s = Server(args.desc_length, args.short_types, args.docstrings)
    s.run()

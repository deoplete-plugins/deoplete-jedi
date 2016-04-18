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
import re
import sys
import struct
import logging
import argparse
import functools
import subprocess
from glob import glob

# This is be possible because the path is inserted in deoplete_jedi.py as well
# as set in PYTHONPATH by the Client class.
from deoplete_jedi import utils

log = logging.getLogger('server')
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
    if not len(header):
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


def strip_decor(source):
    """Remove decorators lines

    If the decorator is a function call, this will leave them dangling.  Jedi
    should be fine with this since they'll look like tuples just hanging out
    not doing anything important.
    """
    return re.sub(r'^(\s*)@\w+', r'\1', source, flags=re.M)


def retry_completion(func):
    """Decorator to retry a completion

    A second attempt is made with decorators stripped from the source.
    """
    @functools.wraps(func)
    def wrapper(self, source, *args, **kwargs):
        try:
            return func(self, source, *args, **kwargs)
        except Exception:
            if '@' in source:
                log.warn('Retrying completion %r', func.__name__)
                try:
                    return func(self, strip_decor(source), *args, **kwargs)
                except:
                    pass
            log.warn('Failed completion %r', func.__name__)
    return wrapper


class Server(object):
    """Server class

    This is created when this script is ran directly.
    """
    def __init__(self, desc_len=0, short_types=False, show_docstring=False):
        self.desc_len = desc_len
        self.use_short_types = short_types
        self.show_docstring = show_docstring

        from jedi import settings
        settings.use_filesystem_cache = False

    def _loop(self):
        from jedi.evaluate.sys_path import _get_venv_sitepackages

        while True:
            data = stream_read(sys.stdin)
            if not isinstance(data, tuple):
                continue

            cache_key, source, line, col, filename = data
            orig_path = sys.path[:]
            venv = os.getenv('VIRTUAL_ENV')
            if venv:
                sys.path.insert(0, _get_venv_sitepackages(venv))
            add_path = self.find_extra_sys_path(filename)
            if add_path and add_path not in sys.path:
                # Add the found path to sys.path.  I'm not 100% certain if this
                # is actually helping anything, but it feels like the right
                # thing to do.
                sys.path.insert(0, add_path)
            if filename:
                sys.path.append(os.path.dirname(filename))

            # Decorators on incomplete functions cause an error to be raised by
            # Jedi.  I assume this is because Jedi is attempting to evaluate
            # the return value of the wrapped, but broken, function.
            # Our solution is to simply strip decorators from the source since
            # we are a completion service, not the syntax police.
            out = None

            if cache_key[-1] == 'vars':
                # Attempt scope completion.  If it fails, it should fall
                # through to script completion.
                out = self.scoped_completions(source, filename, cache_key[-2])

            if not out:
                out = self.script_completion(source, line, col, filename)

            if not out and cache_key[-1] in ('package', 'local'):
                # The backup plan
                try:
                    out = self.module_completions(cache_key[0], sys.path)
                except Exception:
                    pass

            stream_write(sys.stdout, out)
            sys.path[:] = orig_path

    def run(self):
        log.debug('Starting server.  sys.path = %r', sys.path)
        try:
            stream_write(sys.stdout, tuple(sys.version_info))
            self._loop()
        except StreamEmpty:
            log.debug('Input closed.  Shutting down.')
        except Exception:
            log.exception('Server Exception.  Shutting down.')

    def find_extra_sys_path(self, filename):
        """Find the file's "root"

        This tries to determine the script's root package.  The first step is
        to scan upward until there are no longer __init__.py files.  If that
        fails, check immediate subdirectories to find __init__.py files which
        could mean that the current script is not part of a package, but has
        sub-modules.
        """
        add_path = ''
        dirname = os.path.dirname(filename)
        scan_dir = dirname
        while len(scan_dir) \
                and os.path.isfile(os.path.join(scan_dir, '__init__.py')):
            scan_dir = os.path.dirname(scan_dir)

        if scan_dir != dirname:
            add_path = scan_dir
        elif glob('{}/*/__init__.py'.format(dirname)):
            add_path = dirname

        return add_path

    def module_completions(self, module, paths):
        """Directly get completions from the module file

        This is the fallback if all else fails for module completion.
        """
        found = utils.module_search(module, paths)
        if not found:
            return None

        log.debug('Found script for fallback completions: %r', found)
        mod_parts = tuple(re.sub(r'\.+', '.', module).strip('.').split('.'))
        path_parts = os.path.splitext(found)[0].split('/')
        if path_parts[-1] == '__init__':
            path_parts.pop()
        path_parts = tuple(path_parts)
        match_mod = mod_parts
        ml = len(mod_parts)
        for i in range(ml):
            if path_parts[i-ml:] == mod_parts[:ml-i]:
                match_mod = mod_parts[-i:]
                break
        log.debug('Remainder to match: %r', match_mod)

        import jedi
        completions = jedi.api.names(path=found, references=True)
        completions = utils.jedi_walk(completions)
        while len(match_mod):
            for c in completions:
                if c.name == match_mod[0]:
                    completions = c.defined_names()
                    break
            else:
                log.debug('No more matches at %r', match_mod[0])
                return []
            match_mod = match_mod[:-1]

        out = []
        tmp_filecache = {}
        seen = set()
        for c in completions:
            name, type_, desc, abbr = self.parse_completion(c, tmp_filecache)
            seen_key = (type_, name)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            kind = type_ if not self.use_short_types \
                else _types.get(type_) or type_
            out.append((c.module_path, name, type_, desc, abbr, kind))
        return out

    @retry_completion
    def script_completion(self, source, line, col, filename):
        """Standard Jedi completions"""
        import jedi
        log.debug('Line: %r, Col: %r, Filename: %r', line, col, filename)
        completions = jedi.Script(source, line, col, filename).completions()
        out = []
        tmp_filecache = {}
        for c in completions:
            name, type_, desc, abbr = self.parse_completion(c, tmp_filecache)
            kind = type_ if not self.use_short_types \
                else _types.get(type_) or type_
            out.append((c.module_path, name, type_, desc, abbr, kind))
        return out

    def get_parents(self, c):
        """Collect parent blocks

        This is for matching a request's cache key when performing scoped
        completions.
        """
        parents = []
        while True:
            try:
                c = c.parent()
                parents.insert(0, c.name)
                if c.type == 'module':
                    break
            except AttributeError:
                break
        return tuple(parents)

    @retry_completion
    def scoped_completions(self, source, filename, parent):
        """Scoped completion

        This gets all definitions for a specific scope allowing them to be
        cached without needing to consider the current position in the source.
        This would be slow in Vim without threading.
        """
        import jedi
        completions = jedi.api.names(source, filename, all_scopes=True)
        out = []
        tmp_filecache = {}
        seen = set()
        for c in completions:
            c_parents = self.get_parents(c)
            if parent and (len(c_parents) > len(parent) or
                           c_parents != parent[:len(c_parents)]):
                continue
            name, type_, desc, abbr = self.parse_completion(c, tmp_filecache)
            seen_key = (type_, name)
            if seen_key in seen:
                continue
            seen.add(seen_key)
            kind = type_ if not self.use_short_types \
                else _types.get(type_) or type_
            out.append((c.module_path, name, type_, desc, abbr, kind))
        return out

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
        from jedi.api.classes import Completion
        name = comp.name

        if isinstance(comp, Completion):
            type_, desc = [x.strip() for x in comp.description.split(':', 1)]
        else:
            type_ = comp.type
            desc = comp.description

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
        self.version = (0, 0, 0, 'final', 0)
        self.env = os.environ.copy()
        self.env.update({
            'PYTHONPATH': ':'.join((jedi_path,
                                    os.path.dirname(os.path.dirname(__file__)))),
        })

        prog = 'python'
        if 'VIRTUAL_ENV' in os.environ:
            self.env['VIRTUAL_ENV'] = os.getenv('VIRTUAL_ENV')
            prog = os.path.join(self.env['VIRTUAL_ENV'], 'bin', 'python')

        self.cmd = [prog, '-u', __file__, '--desc-length', str(desc_len)]
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
        self.version = stream_read(self._server.stdout)

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

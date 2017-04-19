"""Jedi mini server for deoplete-jedi

This script allows Jedi to run using the Python interpreter that is found in
the user's environment instead of the one Neovim is using.

Jedi seems to accumulate latency with each completion.  To deal with this, the
server is restarted after 50 completions.  This threshold is relatively high
considering that deoplete-jedi caches completion results.  These combined
should make deoplete-jedi's completions pretty fast and responsive.
"""
from __future__ import unicode_literals

import argparse
import functools
import logging
import os
import re
import struct
import subprocess
import sys
import threading
import time
from glob import glob

# This is be possible because the path is inserted in deoplete_jedi.py as well
# as set in PYTHONPATH by the Client class.
from deoplete_jedi import utils

log = logging.getLogger('deoplete')
nullHandler = logging.NullHandler()

if not log.handlers:
    log.addHandler(nullHandler)

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
    header = struct.pack(b'I', len(data))
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
                log.warn('Retrying completion %r', func.__name__, exc_info=True)
                try:
                    return func(self, strip_decor(source), *args, **kwargs)
                except Exception:
                    pass
            log.warn('Failed completion %r', func.__name__, exc_info=True)
    return wrapper


class Server(object):
    """Server class

    This is created when this script is ran directly.
    """
    def __init__(self, desc_len=0, short_types=False, show_docstring=False):
        self.desc_len = desc_len
        self.use_short_types = short_types
        self.show_docstring = show_docstring
        self.unresolved_imports = set()

        from jedi import settings

        settings.use_filesystem_cache = False

    def _loop(self):
        from jedi.evaluate.sys_path import _get_venv_sitepackages

        while True:
            data = stream_read(sys.stdin)
            if not isinstance(data, tuple):
                continue

            cache_key, source, line, col, filename, options = data
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

            if isinstance(options, dict):
                extra = options.get('extra_path')
                if extra:
                    if not isinstance(extra, list):
                        extra = [extra]
                    sys.path.extend(extra)

                # Add extra paths if working on a Python remote plugin.
                sys.path.extend(utils.rplugin_runtime_paths(options))

            # Decorators on incomplete functions cause an error to be raised by
            # Jedi.  I assume this is because Jedi is attempting to evaluate
            # the return value of the wrapped, but broken, function.
            # Our solution is to simply strip decorators from the source since
            # we are a completion service, not the syntax police.
            out = self.script_completion(source, line, col, filename)

            if not out and cache_key[-1] == 'vars':
                # Attempt scope completion.  If it fails, it should fall
                # through to script completion.
                log.debug('Fallback to scoped completions')
                out = self.scoped_completions(source, filename, cache_key[-2])

            if not out and isinstance(options, dict) and 'synthetic' in options:
                synthetic = options.get('synthetic')
                log.debug('Using synthetic completion: %r', synthetic)
                out = self.script_completion(synthetic['src'],
                                             synthetic['line'],
                                             synthetic['col'], filename)

            if not out and cache_key[-1] in ('package', 'local'):
                # The backup plan
                log.debug('Fallback to module completions')
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
            parsed = self.parse_completion(c, tmp_filecache)
            seen_key = (parsed['type'], parsed['name'])
            if seen_key in seen:
                continue
            seen.add(seen_key)
            out.append(parsed)
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
            out.append(self.parse_completion(c, tmp_filecache))
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

    def resolve_import(self, completion, depth=0, max_depth=10, seen=None):
        """Follow import until it no longer is an import type"""
        if seen is None:
            seen = []
        seen.append(completion)
        log.debug('Resolving: %r', completion)
        defs = completion.goto_assignments()
        if not defs:
            return None
        resolved = defs[0]
        if resolved in seen:
            return None
        if resolved.type == 'import' and depth < max_depth:
            return self.resolve_import(resolved, depth + 1, max_depth, seen)
        log.debug('Resolved: %r', resolved)
        return resolved

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
            if c.type == 'import' and c.full_name not in self.unresolved_imports:
                resolved = self.resolve_import(c)
                if resolved is None:
                    log.debug('Could not resolve import: %r', c.full_name)
                    self.unresolved_imports.add(c.full_name)
                    continue
                else:
                    c = resolved
            parsed = self.parse_completion(c, tmp_filecache)
            seen_key = (parsed['name'], parsed['type'])
            if seen_key in seen:
                continue
            seen.add(seen_key)
            out.append(parsed)
        return out

    def completion_dict(self, name, type_, comp):
        """Final construction of the completion dict."""
        doc = comp.docstring()
        i = doc.find('\n\n')
        if i != -1:
            doc = doc[i:]

        params = None
        try:
            if type_ in ('function', 'class'):
                params = []
                for i, p in enumerate(comp.params):
                    desc = p.description.strip()
                    if i == 0 and desc == 'self':
                        continue
                    if '\\n' in desc:
                        desc = desc.replace('\\n', '\\x0A')
                    # Note: Hack for jedi param bugs
                    if desc.startswith('param ') or desc == 'param':
                        desc = desc[5:].strip()
                    if desc:
                        params.append(desc)
        except Exception:
            params = None

        return {
            'module': comp.module_path,
            'name': name,
            'type': type_,
            'short_type': _types.get(type_),
            'doc': doc.strip(),
            'params': params,
        }

    def parse_completion(self, comp, cache):
        """Return a tuple describing the completion.

        Returns (name, type, description, abbreviated)
        """
        name = comp.name

        type_ = comp.type
        desc = comp.description

        if type_ == 'instance' and desc.startswith(('builtins.', 'posix.')):
            # Simple description
            builtin_type = desc.rsplit('.', 1)[-1]
            if builtin_type in _types:
                return self.completion_dict(name, builtin_type, comp)

        if type_ == 'class' and desc.startswith('builtins.'):
            return self.completion_dict(name, type_, comp)

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
                        return self.completion_dict(name, 'property', comp)
                    i -= 1
            return self.completion_dict(name, type_, comp)

        return self.completion_dict(name, type_, comp)


class Client(object):
    """Client object

    This will be used by deoplete-jedi to interact with the server.
    """
    max_completion_count = 50

    def __init__(self, desc_len=0, short_types=False, show_docstring=False,
                 debug=False, python_path=None):
        self._server = None
        self.restarting = threading.Lock()
        self.version = (0, 0, 0, 'final', 0)
        self.env = os.environ.copy()
        self.env.update({
            'PYTHONPATH': os.pathsep.join(
                (jedi_path, os.path.dirname(os.path.dirname(__file__)))),
        })

        if not python_path:
            prog = 'python'
        else:
            prog = python_path

        if 'VIRTUAL_ENV' in os.environ:
            self.env['VIRTUAL_ENV'] = os.getenv('VIRTUAL_ENV')
            prog = os.path.join(self.env['VIRTUAL_ENV'], 'bin', 'python')

        self.cmd = [prog, '-u', __file__, '--desc-length', str(desc_len)]
        if short_types:
            self.cmd.append('--short-types')
        if show_docstring:
            self.cmd.append('--docstrings')
        if debug:
            self.cmd.extend(('--debug', debug[0], '--debug-level',
                             str(debug[1])))

        try:
            self.restart()
        except Exception as exc:
            from deoplete.exceptions import SourceInitError
            raise SourceInitError('Failed to start server ({}): {}'.format(
                ' '.join(self.cmd), exc))

    def shutdown(self):
        """Shut down the server."""
        if self._server is not None and self._server.returncode is None:
            # Closing the server's stdin will cause it to exit.
            self._server.stdin.close()
            self._server.kill()

    def restart(self):
        """Start or restart the server

        If a server is already running, shut it down.
        """
        with self.restarting:
            self.shutdown()
            self._server = subprocess.Popen(self.cmd, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE,
                                            env=self.env)
            # Might result in "pyenv: version `foo' is not installed (set by
            # /cwd/.python-version)" on stderr.
            try:
                self.version = stream_read(self._server.stdout)
            except StreamEmpty:
                out, err = self._server.communicate()
                raise Exception('Server exited with {}: error: {}'.format(
                    err, self._server.returncode))
            self._count = 0

    def completions(self, *args):
        """Get completions from the server.

        If the number of completions already performed reaches a threshold,
        restart the server.
        """
        if self._count > self.max_completion_count:
            self.restart()

        self._count += 1
        try:
            stream_write(self._server.stdin, args)
            return stream_read(self._server.stdout)
        except StreamError as exc:
            if self.restarting.acquire(False):
                self.restarting.release()
                log.error('Caught %s during handling completions(%s), '
                          ' restarting server', exc, args)
                self.restart()
                time.sleep(0.2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--desc-length', type=int)
    parser.add_argument('--short-types', action='store_true')
    parser.add_argument('--docstrings', action='store_true')
    parser.add_argument('--debug', default='')
    parser.add_argument('--debug-level', type=int, default=logging.DEBUG)
    args = parser.parse_args()

    if args.debug:
        log.removeHandler(nullHandler)
        formatter = logging.Formatter('%(asctime)s %(levelname)-8s '
                                      '(%(name)s) %(message)s')
        handler = logging.FileHandler(args.debug)
        handler.setFormatter(formatter)
        handler.setLevel(args.debug_level)
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        log = log.getChild('jedi.server')

    s = Server(args.desc_length, args.short_types, args.docstrings)
    s.run()
else:
    log = log.getChild('jedi.client')

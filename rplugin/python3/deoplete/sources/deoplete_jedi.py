import logging
import os
import re
import sys
import time

sys.path.insert(1, os.path.dirname(__file__)) # noqa: E261
from deoplete_jedi import cache, profiler, utils, worker

from .base import Base


def sort_key(item):
    w = item.get('name')
    l = len(w)
    z = l - len(w.lstrip('_'))
    return (('z' * z) + w.lower()[z:], len(w))


class Source(Base):

    def __init__(self, vim):
        Base.__init__(self, vim)
        self.name = 'jedi'
        self.mark = '[jedi]'
        self.rank = 500
        self.filetypes = ['python', 'cython', 'pyrex']
        self.input_pattern = (r'[\w\)\]\}\'\"]+\.\w*$|'
                              r'^\s*@\w*$|'
                              r'^\s*from\s+[\w\.]*(?:\s+import\s+(?:\w*(?:,\s*)?)*)?|'
                              r'^\s*import\s+(?:[\w\.]*(?:,\s*)?)*')
        self._async_keys = set()
        self.workers_started = False

    def on_init(self, context):
        vars = context['vars']

        self.statement_length = vars.get(
            'deoplete#sources#jedi#statement_length', 0)
        self.server_timeout = vars.get(
            'deoplete#sources#jedi#server_timeout', 10)
        self.use_short_types = vars.get(
            'deoplete#sources#jedi#short_types', False)
        self.show_docstring = vars.get(
            'deoplete#sources#jedi#show_docstring', False)
        self.debug_server = vars.get(
            'deoplete#sources#jedi#debug_server', None)
        # Only one worker is really needed since deoplete-jedi has a pretty
        # aggressive cache.
        # Two workers may be needed if working with very large source files.
        self.worker_threads = vars.get(
            'deoplete#sources#jedi#worker_threads', 2)
        # Hard coded python interpreter location
        self.python_path = vars.get(
            'deoplete#sources#jedi#python_path', '')
        self.extra_path = vars.get(
            'deoplete#sources#jedi#extra_path', [])

        self.boilerplate = []  # Completions that are included in all results

        log_file = ''
        root_log = logging.getLogger('deoplete')

        if self.debug_server is not None and self.debug_server:
            self.debug_enabled = True
            if isinstance(self.debug_server, str):
                log_file = self.debug_server
            else:
                for handler in root_log.handlers:
                    if isinstance(handler, logging.FileHandler):
                        log_file = handler.baseFilename
                        break

        if not self.debug_enabled:
            child_log = root_log.getChild('jedi')
            child_log.propagate = False

        if not self.workers_started:
            if self.python_path and 'VIRTUAL_ENV' not in os.environ:
                cache.python_path = self.python_path
            worker.start(max(1, self.worker_threads), self.statement_length,
                         self.server_timeout, self.use_short_types, self.show_docstring,
                         (log_file, root_log.level), self.python_path)
            cache.start_background(worker.comp_queue)
            self.workers_started = True

    def get_complete_position(self, context):
        pattern = r'\w*$'
        if context['input'].lstrip().startswith(('from ', 'import ')):
            m = re.search(r'[,\s]$', context['input'])
            if m:
                return m.end()
        m = re.search(pattern, context['input'])
        return m.start() if m else -1

    def mix_boilerplate(self, completions):
        seen = set()
        for item in self.boilerplate + completions:
            if item['name'] in seen:
                continue
            seen.add(item['name'])
            yield item

    def finalize(self, item):
        abbr = item['name']

        if self.show_docstring:
            desc = item['doc']
        else:
            desc = ''

        if item['params'] is not None:
            sig = '{}({})'.format(item['name'], ', '.join(item['params']))
            sig_len = len(sig)

            desc = sig + '\n\n' + desc

            if self.statement_length > 0 and sig_len > self.statement_length:
                params = []
                length = len(item['name']) + 2

                for p in item['params']:
                    p = p.split('=', 1)[0]
                    length += len(p)
                    params.append(p)

                length += 2 * (len(params) - 1)

                # +5 for the ellipsis and separator
                while length + 5 > self.statement_length and len(params):
                    length -= len(params[-1]) + 2
                    params = params[:-1]

                if len(item['params']) > len(params):
                    params.append('...')

                sig = '{}({})'.format(item['name'], ', '.join(params))

            abbr = sig

        if self.use_short_types:
            kind = item['short_type'] or item['type']
        else:
            kind = item['type']

        return {
            'word': item['name'],
            'abbr': abbr,
            'kind': kind,
            'info': desc.strip(),
            'menu': '[jedi] ',
            'dup': 1,
        }

    def finalize_cached(self, cache_key, filters, cached):
        if cached:
            if cached.completions is None:
                out = self.mix_boilerplate([])
            elif cache_key[-1] == 'vars':
                out = self.mix_boilerplate(cached.completions)
            else:
                out = cached.completions
            if filters:
                out = (x for x in out if x['type'] in filters)
            return [self.finalize(x) for x in sorted(out, key=sort_key)]
        return []

    @profiler.profile
    def gather_candidates(self, context):
        refresh_boilerplate = False
        if not self.boilerplate:
            bp = cache.retrieve(('boilerplate~',))
            if bp:
                self.boilerplate = bp.completions[:]
                refresh_boilerplate = True
            else:
                # This should be the first time any completion happened, so
                # `wait` will be True.
                worker.work_queue.put((('boilerplate~',), [], '', 1, 0, '', None))

        line = context['position'][1]
        col = context['complete_position']
        buf = self.vim.current.buffer
        src = buf[:]

        extra_modules = []
        cache_key = None
        cached = None
        refresh = True
        wait = False

        # Inclusion filters for the results
        filters = []

        if re.match('^\s*(from|import)\s+', context['input']) \
                and not re.match('^\s*from\s+\S+\s+', context['input']):
            # If starting an import, only show module results
            filters.append('module')

        cache_key, extra_modules = cache.cache_context(buf.name, context, src,
                                                       self.extra_path)
        cached = cache.retrieve(cache_key)
        if cached and not cached.refresh:
            modules = cached.modules
            if all([filename in modules for filename in extra_modules]) \
                    and all([utils.file_mtime(filename) == mtime
                             for filename, mtime in modules.items()]):
                # The cache is still valid
                refresh = False

        if cache_key and (cache_key[-1] in ('dot', 'vars', 'import', 'import~') or
                          (cached and cache_key[-1] == 'package' and
                           not len(cached.modules))):
            # Always refresh scoped variables and module imports.  Additionally
            # refresh cached items that did not have associated module files.
            refresh = True

        # Extra options to pass to the server.
        options = {
            'cwd': context.get('cwd'),
            'extra_path': self.extra_path,
            'runtimepath': context.get('runtimepath'),
        }

        if (not cached or refresh) and cache_key and cache_key[-1] == 'package':
            # Create a synthetic completion for a module import as a fallback.
            synthetic_src = ['import {0}; {0}.'.format(cache_key[0])]
            options.update({
                'synthetic': {
                    'src': synthetic_src,
                    'line': 1,
                    'col': len(synthetic_src[0]),
                }
            })

        if not cached:
            wait = True

        # Note: This waits a very short amount of time to give the server or
        # cache a chance to reply.  If there's no reply during this period,
        # empty results are returned and we defer to deoplete's async refresh.
        # The current requests's async status is tracked in `_async_keys`.
        # If the async cache result is older than 5 seconds, the completion
        # request goes back to the default behavior of attempting to refresh as
        # needed by the `refresh` and `wait` variables above.
        self.debug('Key: %r, Refresh: %r, Wait: %r, Async: %r', cache_key,
                   refresh, wait, cache_key in self._async_keys)

        context['is_async'] = cache_key in self._async_keys
        if context['is_async']:
            if not cached:
                self.debug('[async] waiting for completions: %r', cache_key)
                return []
            else:
                self._async_keys.remove(cache_key)
                context['is_async'] = False
                if time.time() - cached.time < 5:
                    self.debug('[async] finished: %r', cache_key)
                    return self.finalize_cached(cache_key, filters, cached)
                else:
                    self.debug('[async] outdated: %r', cache_key)

        if cache_key and (not cached or refresh):
            n = time.time()
            wait_complete = False
            worker.work_queue.put((cache_key, extra_modules, '\n'.join(src),
                                   line, col, str(buf.name), options))
            while wait and time.time() - n < 0.25:
                cached = cache.retrieve(cache_key)
                if cached and cached.time >= n:
                    self.debug('Got updated cache, stopped waiting.')
                    wait_complete = True
                    break
                time.sleep(0.01)

            if wait and not wait_complete:
                self._async_keys.add(cache_key)
                context['is_async'] = True
                self.debug('[async] deferred: %r', cache_key)
                return []

        if refresh_boilerplate:
            # This should only occur the first time completions happen.
            # Refresh the boilerplate to ensure it's always up to date (just in
            # case).
            self.debug('Refreshing boilerplate')
            worker.work_queue.put((('boilerplate~',), [], '', 1, 0, '', None))

        return self.finalize_cached(cache_key, filters, cached)
